"""Test harness — no Docker, no Redis, no PostgreSQL, no network.

* ``ATLAS_MOCK_BROKERS=true`` is forced before any app module is imported, so
  both broker clients build and sign real payloads but never open a socket.
* Redis is replaced by :class:`FakeRedis`, an in-memory stub implementing only
  the handful of commands ``app.core.redis_client`` uses (including real TTL
  semantics so the expiry test is meaningful).
* The audit ledger stays disabled, exercising the "degrade, don't fail" paths.
"""

from __future__ import annotations

import fnmatch
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

# ── import path + environment, BEFORE any `app.*` import ───────────────────
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("ATLAS_ENV", "test")
os.environ["ATLAS_MOCK_BROKERS"] = "true"
os.environ["ATLAS_WEBHOOK_TOKEN"] = "test_token"
os.environ["ATLAS_WEBHOOK_HMAC_SECRET"] = "test_hmac_secret"
os.environ["ATLAS_CONFIRMATION_TTL_SECONDS"] = "60"
os.environ["ATLAS_MAX_CONTRACTS"] = "4"
os.environ["ATLAS_MAX_RISK_PCT"] = "5.0"
os.environ["ATLAS_MAX_DAILY_DRAWDOWN_PCT"] = "3.0"
os.environ["ATLAS_MAX_CONCURRENT_POSITIONS"] = "3"
os.environ["ATLAS_ACCOUNT_EQUITY"] = "100000"
os.environ["ATLAS_TRADOVATE_ACCOUNT_SPEC"] = "ACCOUNT_DEMO"
os.environ["ATLAS_TRADOVATE_ACCOUNT_ID"] = "12345"
os.environ["ATLAS_KRAKEN_DEFAULT_LEVERAGE"] = "3"
# Empty FCM config → the push engine degrades to a mock message id.
os.environ["ATLAS_FCM_PROJECT_ID"] = ""
os.environ["ATLAS_FCM_SERVICE_ACCOUNT_JSON"] = ""

from app.core import db, redis_client  # noqa: E402
from app.core.config import get_settings, reset_settings_cache  # noqa: E402


# ── In-memory async Redis stub (no fakeredis dependency) ───────────────────
class FakeRedis:
    """Implements exactly the commands ``app.core.redis_client`` issues."""

    def __init__(self) -> None:
        # key -> (value, expires_at_monotonic | None)
        self._store: Dict[str, Tuple[str, Optional[float]]] = {}
        self.reachable = True

    # -- internals --
    def _alive(self, key: str) -> bool:
        entry = self._store.get(key)
        if entry is None:
            return False
        _, expires_at = entry
        if expires_at is not None and time.monotonic() >= expires_at:
            self._store.pop(key, None)
            return False
        return True

    def _guard(self) -> None:
        if not self.reachable:
            raise ConnectionError("FakeRedis: simulated outage")

    # -- commands --
    async def ping(self) -> bool:
        self._guard()
        return True

    async def set(self, key: str, value: str) -> bool:
        self._guard()
        self._store[key] = (value, None)
        return True

    async def setex(self, key: str, ttl: int, value: str) -> bool:
        self._guard()
        self._store[key] = (value, time.monotonic() + float(ttl))
        return True

    async def get(self, key: str) -> Optional[str]:
        self._guard()
        return self._store[key][0] if self._alive(key) else None

    async def delete(self, *keys: str) -> int:
        self._guard()
        removed = 0
        for key in keys:
            if self._alive(key):
                removed += 1
            self._store.pop(key, None)
        return removed

    async def scan(
        self, cursor: int = 0, match: str = "*", count: int = 100
    ) -> Tuple[int, List[str]]:
        self._guard()
        live = [k for k in list(self._store) if self._alive(k)]
        return 0, [k for k in live if fnmatch.fnmatch(k, match)]

    async def aclose(self) -> None:
        return None

    # -- test helpers --
    def expire_now(self, key: str) -> None:
        """Force a key past its TTL without sleeping."""
        if key in self._store:
            value, _ = self._store[key]
            self._store[key] = (value, time.monotonic() - 1.0)

    def expire_all(self, prefix: str = "") -> None:
        for key in list(self._store):
            if key.startswith(prefix):
                self.expire_now(key)

    def clear(self) -> None:
        self._store.clear()
        self.reachable = True


# ── Broker spy ─────────────────────────────────────────────────────────────
class SpyBroker:
    """Records every call so a test can assert *zero* broker interaction."""

    def __init__(self, venue: str, inner: Any = None) -> None:
        self.venue = venue
        self.inner = inner
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def calls_named(self, name: str) -> List[Dict[str, Any]]:
        return [kwargs for method, kwargs in self.calls if method == name]

    async def place_bracket_order(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(("place_bracket_order", kwargs))
        if self.inner is not None:
            return await self.inner.place_bracket_order(**kwargs)
        return {"orderId": 1, "mock": True, "payload": kwargs}

    async def cancel_all(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(("cancel_all", kwargs))
        if self.inner is not None:
            return await self.inner.cancel_all(**kwargs)
        return {"venue": self.venue, "mock": True}

    async def flatten_all(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(("flatten_all", kwargs))
        if self.inner is not None:
            return await self.inner.flatten_all(**kwargs)
        return {"venue": self.venue, "mock": True}

    async def get_account(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(("get_account", kwargs))
        if self.inner is not None:
            return await self.inner.get_account(**kwargs)
        return {"venue": self.venue, "mock": True}

    async def aclose(self) -> None:
        return None


# ── Fixtures ───────────────────────────────────────────────────────────────
@pytest.fixture(scope="session", autouse=True)
def _settings() -> Any:
    reset_settings_cache()
    settings = get_settings()
    assert settings.is_mock, "tests must run with ATLAS_MOCK_BROKERS=true"
    return settings


@pytest.fixture
def settings(_settings: Any) -> Any:
    return _settings


@pytest.fixture
def fake_redis() -> Any:
    """Install the stub as the process-wide Redis client for one test."""
    stub = FakeRedis()
    redis_client.set_client(stub)
    try:
        yield stub
    finally:
        stub.clear()
        redis_client.set_client(None)


@pytest.fixture(autouse=True)
def _no_database() -> Any:
    """Keep the audit ledger disabled so no PostgreSQL is required."""
    db.set_sessionmaker(None)
    yield
    db.set_sessionmaker(None)


@pytest.fixture
def spy_brokers() -> Any:
    """Wrap both venue clients in :class:`SpyBroker` for the duration of a test."""
    from app import brokers
    from app.brokers.kraken_client import KrakenClient
    from app.brokers.tradovate_client import TradovateClient

    spies = {
        "TRADOVATE": SpyBroker("TRADOVATE", TradovateClient()),
        "KRAKEN": SpyBroker("KRAKEN", KrakenClient()),
    }
    for venue, spy in spies.items():
        brokers.set_broker(venue, spy)  # type: ignore[arg-type]
    try:
        yield spies
    finally:
        for venue in spies:
            brokers.set_broker(venue, None)


@pytest.fixture
def app(fake_redis: Any) -> Any:
    """The FastAPI app with the lifespan bypassed (no real Redis/DB startup)."""
    from app.main import create_app

    return create_app()


@pytest.fixture
def client(app: Any) -> Any:
    """Synchronous TestClient that does NOT run the lifespan."""
    from starlette.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
async def async_client(app: Any) -> Any:
    """httpx ASGITransport client for async tests."""
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://atlas.test") as ac:
        yield ac


@pytest.fixture
def signal_payload() -> Dict[str, Any]:
    """The FROZEN artifact §3 payload, verbatim."""
    return {
        "token": "test_token",
        "symbol": "NQ1!",
        "venue": "TRADOVATE",
        "action": "BUY",
        "limit_price": 20155.25,
        "stop_loss": 20140.00,
        "take_profit_1": 20178.12,
        "take_profit_2": 20201.00,
        "regime": "BULL_TREND_EXPANSION",
        "risk_pct": 1.0,
    }
