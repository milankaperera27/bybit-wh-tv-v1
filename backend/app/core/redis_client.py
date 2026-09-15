"""Async Redis pool + the TTL/kill-switch helpers the HITL flow depends on.

Everything degrades gracefully: when Redis is unreachable the accessor raises,
but :func:`ping` reports ``False`` rather than exploding so ``/health`` can
report *degraded* instead of 500.  Tests inject an in-memory stub through
:func:`set_client`, so no Redis server is required.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# ── Redis key space ────────────────────────────────────────────────────────
PENDING_SETUP_PREFIX = "atlas:setup:"
PENDING_INDEX_KEY = "atlas:setups:index"
KILL_SWITCH_KEY = "atlas:kill_switch"
DAILY_PNL_KEY_PREFIX = "atlas:pnl:"
OPEN_POSITIONS_KEY = "atlas:positions:open"
REGIME_KEY = "atlas:regime:current"

_client: Any = None


def setup_key(signal_id: str) -> str:
    return f"{PENDING_SETUP_PREFIX}{signal_id}"


# ── Pool lifecycle ─────────────────────────────────────────────────────────
async def init_redis(url: Optional[str] = None) -> Any:
    """Create the shared async connection pool (idempotent)."""
    global _client
    if _client is not None:
        return _client
    import redis.asyncio as redis  # imported lazily: tests never need the driver

    settings = get_settings()
    _client = redis.from_url(
        url or settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
        health_check_interval=30,
    )
    return _client


async def close_redis() -> None:
    """Tear the pool down on application shutdown."""
    global _client
    if _client is None:
        return
    try:
        aclose = getattr(_client, "aclose", None) or getattr(_client, "close", None)
        if aclose is not None:
            result = aclose()
            if hasattr(result, "__await__"):
                await result
    except Exception:  # pragma: no cover - shutdown best effort
        logger.warning("redis close failed", exc_info=True)
    finally:
        _client = None


def set_client(client: Any) -> None:
    """Inject a client (the test stub, or a pre-built pool)."""
    global _client
    _client = client


def get_client() -> Any:
    """Return the live client, raising if the pool was never initialised."""
    if _client is None:
        raise RuntimeError("Redis pool not initialised; call init_redis() first")
    return _client


async def ping() -> bool:
    """Reachability probe that never raises."""
    try:
        return bool(await get_client().ping())
    except Exception:
        return False


# ── JSON value helpers ─────────────────────────────────────────────────────
async def setex_json(key: str, ttl_seconds: int, value: Any) -> bool:
    """``SETEX key ttl <json>`` — the 60-second HITL hold (spec §5)."""
    payload = json.dumps(value, default=str, separators=(",", ":"))
    await get_client().setex(key, int(ttl_seconds), payload)
    return True


async def get_json(key: str) -> Optional[Any]:
    """Return the decoded JSON value, or ``None`` when the TTL has elapsed."""
    raw = await get_client().get(key)
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("non-JSON value at redis key %s", key)
        return None


async def delete(key: str) -> int:
    """Delete one key, returning the number of keys removed."""
    return int(await get_client().delete(key))


async def set_json(key: str, value: Any) -> bool:
    await get_client().set(key, json.dumps(value, default=str, separators=(",", ":")))
    return True


async def scan_prefix(prefix: str) -> List[str]:
    """Return every key under ``prefix`` (SCAN, never KEYS on a hot path)."""
    client = get_client()
    found: List[str] = []
    cursor = 0
    while True:
        cursor, batch = await client.scan(cursor=cursor, match=f"{prefix}*", count=200)
        for key in batch:
            found.append(key.decode("utf-8") if isinstance(key, bytes) else key)
        if int(cursor) == 0:
            break
    return found


# ── Pending setup helpers ──────────────────────────────────────────────────
async def store_pending_setup(signal_id: str, setup: Dict[str, Any], ttl_seconds: int) -> bool:
    return await setex_json(setup_key(signal_id), ttl_seconds, setup)


async def load_pending_setup(signal_id: str) -> Optional[Dict[str, Any]]:
    return await get_json(setup_key(signal_id))


async def delete_pending_setup(signal_id: str) -> int:
    return await delete(setup_key(signal_id))


async def list_pending_setups() -> List[Dict[str, Any]]:
    """Every *unexpired* setup.  Expired keys are simply absent from the SCAN."""
    setups: List[Dict[str, Any]] = []
    for key in await scan_prefix(PENDING_SETUP_PREFIX):
        value = await get_json(key)
        if value is not None:
            setups.append(value)
    setups.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    return setups


async def purge_pending_setups() -> int:
    """Drop every held setup — used by the flatten-all emergency route."""
    keys = await scan_prefix(PENDING_SETUP_PREFIX)
    removed = 0
    for key in keys:
        removed += await delete(key)
    return removed


# ── Kill switch helpers ────────────────────────────────────────────────────
async def engage_kill_switch(reason: str = "manual", actor: str = "operator") -> Dict[str, Any]:
    from datetime import datetime, timezone

    payload = {
        "engaged": True,
        "reason": reason,
        "actor": actor,
        "engaged_at": datetime.now(timezone.utc).isoformat(),
    }
    await set_json(KILL_SWITCH_KEY, payload)
    return payload


async def release_kill_switch(actor: str = "operator") -> Dict[str, Any]:
    from datetime import datetime, timezone

    payload = {
        "engaged": False,
        "reason": None,
        "actor": actor,
        "released_at": datetime.now(timezone.utc).isoformat(),
    }
    await set_json(KILL_SWITCH_KEY, payload)
    return payload


async def kill_switch_state() -> Dict[str, Any]:
    """Current flag.  A Redis outage is treated as *not engaged* but flagged."""
    try:
        value = await get_json(KILL_SWITCH_KEY)
    except Exception:
        return {"engaged": False, "reason": None, "degraded": True}
    if not value:
        return {"engaged": False, "reason": None}
    return value


async def is_kill_switch_engaged() -> bool:
    return bool((await kill_switch_state()).get("engaged"))


# ── Daily P&L / open-position counters ─────────────────────────────────────
def daily_pnl_key(day: str) -> str:
    return f"{DAILY_PNL_KEY_PREFIX}{day}"


async def get_daily_pnl(day: str) -> float:
    value = await get_json(daily_pnl_key(day))
    if value is None:
        return 0.0
    if isinstance(value, dict):
        return float(value.get("realized_pnl", 0.0))
    return float(value)


async def set_daily_pnl(day: str, pnl: float) -> bool:
    return await set_json(daily_pnl_key(day), {"realized_pnl": float(pnl)})


async def get_open_position_count() -> int:
    value = await get_json(OPEN_POSITIONS_KEY)
    if value is None:
        return 0
    if isinstance(value, dict):
        return int(value.get("count", 0))
    if isinstance(value, list):
        return len(value)
    return int(value)


async def set_open_position_count(count: int) -> bool:
    return await set_json(OPEN_POSITIONS_KEY, {"count": int(count)})


async def set_current_regime(regime: str) -> bool:
    from datetime import datetime, timezone

    return await set_json(
        REGIME_KEY, {"regime": regime, "as_of": datetime.now(timezone.utc).isoformat()}
    )


async def get_current_regime() -> Optional[Dict[str, Any]]:
    try:
        return await get_json(REGIME_KEY)
    except Exception:
        return None
