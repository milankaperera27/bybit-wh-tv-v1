"""WebSocket frame contract between the backend hub and the PWA parser.

These frames are the one part of the backend the REST acceptance gates never
touched, and the two build tracks drifted: the hub emitted ``pending`` while
``mobile-dashboard/src/api/socket.ts`` reads ``setups``.  Because the client
coerces an unparseable snapshot to an EMPTY list and then *replaces* its state
with it, every snapshot silently wiped the live confirmation cards — with no
console error and a healthy-looking REST response.

Each assertion below mirrors a specific line of `parseDashboardEvent`.
"""

from __future__ import annotations

import pytest

from app.core import redis_client
from app.main import dashboard_hub, kill_switch_frame
from app.core.state_machine import SignalStatus


class TestSnapshotFrame:
    async def test_snapshot_uses_the_key_the_client_parses(self, fake_redis) -> None:
        """socket.ts: `const rawSetups = record['setups']`."""
        frame = await dashboard_hub.snapshot()
        assert frame["type"] == "snapshot"
        assert "setups" in frame, "client reads 'setups'; a missing key wipes the card list"
        assert isinstance(frame["setups"], list)

    async def test_snapshot_carries_live_setups(self, fake_redis) -> None:
        await redis_client.store_pending_setup(
            "abc123", {"signal_id": "abc123", "symbol": "NQ1!"}, 60
        )
        frame = await dashboard_hub.snapshot()
        assert [s["signal_id"] for s in frame["setups"]] == ["abc123"]

    async def test_deprecated_pending_alias_still_matches(self, fake_redis) -> None:
        frame = await dashboard_hub.snapshot()
        assert frame["pending"] == frame["setups"]

    async def test_kill_switch_is_an_object_not_a_bare_bool(self, fake_redis) -> None:
        """socket.ts parseKillSwitch: `typeof record['engaged'] !== 'boolean'` -> null."""
        frame = await dashboard_hub.snapshot()
        kill = frame["kill_switch"]
        assert isinstance(kill, dict), "a bare bool fails parseKillSwitch and is dropped"
        assert isinstance(kill["engaged"], bool)


class TestKillSwitchFrame:
    def test_exposes_both_lockout_spellings(self) -> None:
        frame = kill_switch_frame({"engaged": False, "drawdown_lockout": True})
        assert frame["lockout"] is True          # client field
        assert frame["drawdown_lockout"] is True  # REST field

    def test_supplies_the_configured_drawdown_limit(self) -> None:
        assert kill_switch_frame({"engaged": False})["daily_drawdown_limit_pct"] == pytest.approx(3.0)

    def test_tolerates_an_empty_state(self) -> None:
        frame = kill_switch_frame({})
        assert frame["engaged"] is False and frame["daily_drawdown_pct"] == 0.0


class TestEventFrameNames:
    """Names the client's switch statement accepts; anything else returns null."""

    ACCEPTED = {
        "snapshot", "setup_pending", "setup_new", "setup_resolved",
        "setup_update", "regime", "kill_switch", "heartbeat", "pong",
    }

    @pytest.mark.parametrize(
        "frame_type", ["snapshot", "setup_pending", "setup_resolved", "kill_switch"]
    )
    def test_emitted_names_are_recognised_by_the_client(self, frame_type: str) -> None:
        assert frame_type in self.ACCEPTED

    @pytest.mark.parametrize("stale", ["pending_setup", "routed", "rejected"])
    def test_the_old_names_are_not_client_recognised(self, stale: str) -> None:
        """Guards the exact regression: these silently parsed to null."""
        assert stale not in self.ACCEPTED

    def test_resolved_frame_carries_signal_id_and_status(self) -> None:
        """socket.ts drops a `setup_resolved` lacking either field."""
        frame = {
            "type": "setup_resolved",
            "signal_id": "abc123",
            "status": SignalStatus.ROUTED.value,
        }
        assert frame["signal_id"] and frame["status"]
