"""A malformed Redis record must not take down the whole pending list.

Observed in the wild: the dashboard showed `Backend error 500` with zero cards.
`PendingSetup.model_validate` ran outside the route's try block, so one stale
key — left by an older build, or hand-written into Redis — raised a
ValidationError that FastAPI turned into a 500, hiding every healthy setup.
"""

from __future__ import annotations

import pytest

from app.core import redis_client

GOOD = {
    "signal_id": "good1",
    "status": "PENDING_APPROVAL",
    "symbol": "NQ1!",
    "venue": "TRADOVATE",
    "action": "BUY",
    "regime": "BULL_TREND_EXPANSION",
    "limit_price": 20155.25,
    "stop_loss": 20140.0,
    "take_profit_1": 20178.12,
    "take_profit_2": 20201.0,
    "risk_pct": 1.0,
    "qty": 3.0,
    "risk_amount": 1000.0,
    "stop_distance": 15.25,
    "risk_approved": True,
    "risk_reason": "OK",
    "created_at": "2026-01-01T00:00:00Z",
    "expires_at": "2099-01-01T00:00:00Z",
    "ttl_seconds": 60,
}
# Shape written by an earlier build: missing venue/action/prices/etc.
LEGACY = {"signal_id": "legacy1", "symbol": "NQ1!"}


class TestPendingListResilience:
    async def test_one_bad_record_does_not_500_the_list(self, client, fake_redis) -> None:
        await redis_client.store_pending_setup("legacy1", LEGACY, 60)
        response = client.get("/api/v1/confirmation/pending")
        assert response.status_code == 200, "a stale key must not 500 the list"
        assert response.json() == []

    async def test_healthy_setups_survive_a_bad_neighbour(self, client, fake_redis) -> None:
        """The regression that hid real cards: good and bad in the same list."""
        await redis_client.store_pending_setup("good1", GOOD, 60)
        await redis_client.store_pending_setup("legacy1", LEGACY, 60)
        response = client.get("/api/v1/confirmation/pending")
        assert response.status_code == 200
        returned = response.json()
        assert [s["signal_id"] for s in returned] == ["good1"]

    async def test_several_bad_records_are_all_skipped(self, client, fake_redis) -> None:
        for i in range(3):
            await redis_client.store_pending_setup(f"bad{i}", {"signal_id": f"bad{i}"}, 60)
        await redis_client.store_pending_setup("good1", GOOD, 60)
        response = client.get("/api/v1/confirmation/pending")
        assert response.status_code == 200
        assert len(response.json()) == 1


class TestSingleSetupResilience:
    async def test_unparseable_setup_reads_as_gone_not_500(self, client, fake_redis) -> None:
        await redis_client.store_pending_setup("legacy1", LEGACY, 60)
        response = client.get("/api/v1/confirmation/legacy1")
        assert response.status_code == 410
        assert "unreadable" in response.json()["detail"]

    async def test_a_healthy_setup_still_resolves(self, client, fake_redis) -> None:
        await redis_client.store_pending_setup("good1", GOOD, 60)
        response = client.get("/api/v1/confirmation/good1")
        assert response.status_code == 200
        assert response.json()["symbol"] == "NQ1!"


class TestRedisOutage:
    async def test_pending_list_degrades_to_empty_not_500(self, client, fake_redis) -> None:
        """A Redis outage is an empty list, never a 500."""
        fake_redis.reachable = False
        response = client.get("/api/v1/confirmation/pending")
        assert response.status_code == 200
        assert response.json() == []
