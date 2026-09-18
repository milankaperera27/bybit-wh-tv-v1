"""Universal kill switch + flatten-all (artifact §4)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body

from app.brokers import get_broker
from app.core import redis_client
from app.core.config import get_settings
from app.core.risk_engine import drawdown_breached
from app.models.schemas import FlattenResult, KillSwitchStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/emergency", tags=["emergency"])


@router.post("/kill", response_model=KillSwitchStatus)
async def engage_kill(
    reason: str = Body(default="manual", embed=True),
    actor: str = Body(default="operator", embed=True),
) -> KillSwitchStatus:
    """Engage the global halt: no new signals, no new routing."""
    try:
        state = await redis_client.engage_kill_switch(reason=reason, actor=actor)
    except Exception:
        logger.error("could not persist kill switch", exc_info=True)
        state = {"engaged": True, "reason": reason, "actor": actor, "degraded": True}
    await _broadcast({"type": "kill_switch", "kill_switch": _kill_frame(state)})
    return await _status(state)


@router.post("/release", response_model=KillSwitchStatus)
async def release_kill(actor: str = Body(default="operator", embed=True)) -> KillSwitchStatus:
    """Disengage the global halt."""
    try:
        state = await redis_client.release_kill_switch(actor=actor)
    except Exception:
        logger.error("could not persist kill switch release", exc_info=True)
        state = {"engaged": False, "actor": actor, "degraded": True}
    await _broadcast({"type": "kill_switch", "kill_switch": _kill_frame(state)})
    return await _status(state)


@router.post("/flatten", response_model=FlattenResult)
async def flatten_all(
    reason: str = Body(default="emergency_flatten", embed=True),
    actor: str = Body(default="operator", embed=True),
) -> FlattenResult:
    """Cancel working orders + flatten positions on BOTH venues, purge Redis.

    The kill switch is engaged first so nothing can be routed mid-flatten.
    """
    try:
        await redis_client.engage_kill_switch(reason=reason, actor=actor)
        kill_ok = True
    except Exception:
        logger.error("flatten: kill switch write failed", exc_info=True)
        kill_ok = False

    tradovate = await _flatten_venue("TRADOVATE")
    kraken = await _flatten_venue("KRAKEN")

    try:
        purged = await redis_client.purge_pending_setups()
    except Exception:
        logger.error("flatten: pending purge failed", exc_info=True)
        purged = 0

    await _broadcast({"type": "flatten", "reason": reason, "pending_purged": purged})
    return FlattenResult(
        tradovate=tradovate,
        kraken=kraken,
        pending_purged=purged,
        kill_switch_engaged=kill_ok,
    )


@router.get("/status", response_model=KillSwitchStatus)
async def emergency_status() -> KillSwitchStatus:
    """Kill-switch flag + daily drawdown state."""
    try:
        state = await redis_client.kill_switch_state()
    except Exception:
        state = {"engaged": False, "degraded": True}
    return await _status(state)


# ── helpers ────────────────────────────────────────────────────────────────
async def _flatten_venue(venue: str) -> Dict[str, Any]:
    """Cancel then flatten one venue; never raises."""
    result: Dict[str, Any] = {"venue": venue}
    try:
        client = get_broker(venue)
    except Exception as exc:
        return {"venue": venue, "ok": False, "error": str(exc)}
    try:
        result["cancel_all"] = await client.cancel_all()
    except Exception as exc:
        logger.error("flatten: %s cancel_all failed", venue, exc_info=True)
        result["cancel_all"] = {"ok": False, "error": str(exc)}
    try:
        result["flatten_all"] = await client.flatten_all()
    except Exception as exc:
        logger.error("flatten: %s flatten_all failed", venue, exc_info=True)
        result["flatten_all"] = {"ok": False, "error": str(exc)}
    result["ok"] = not (
        isinstance(result.get("cancel_all"), dict) and result["cancel_all"].get("ok") is False
    )
    return result


async def _status(state: Optional[Dict[str, Any]] = None) -> KillSwitchStatus:
    settings = get_settings()
    state = dict(state or {})
    day = datetime.now(timezone.utc).date().isoformat()

    degraded = bool(state.pop("degraded", False))
    try:
        daily_pnl = await redis_client.get_daily_pnl(day)
        open_positions = await redis_client.get_open_position_count()
        pending = len(await redis_client.list_pending_setups())
    except Exception:
        daily_pnl, open_positions, pending = 0.0, 0, 0
        degraded = True

    equity = settings.account_equity
    drawdown_pct = (abs(daily_pnl) / equity * 100.0) if (equity > 0 and daily_pnl < 0) else 0.0

    return KillSwitchStatus(
        engaged=bool(state.get("engaged", False)),
        reason=state.get("reason"),
        actor=state.get("actor"),
        engaged_at=state.get("engaged_at"),
        released_at=state.get("released_at"),
        daily_pnl=daily_pnl,
        daily_drawdown_pct=round(drawdown_pct, 4),
        drawdown_lockout=drawdown_breached(daily_pnl, equity, settings.max_daily_drawdown_pct),
        open_positions=open_positions,
        pending_setups=pending,
        degraded=degraded,
    )


def _kill_frame(state: Dict[str, Any]) -> Dict[str, Any]:
    """Kill-switch frame shaped for the PWA parser (imported lazily: app.main
    imports this router, so a module-level import would be circular)."""
    from app.main import kill_switch_frame

    return kill_switch_frame(state)


async def _broadcast(frame: Dict[str, Any]) -> None:
    try:
        from app.main import dashboard_hub

        await dashboard_hub.broadcast(frame)
    except Exception:  # pragma: no cover
        logger.debug("dashboard broadcast skipped", exc_info=True)
