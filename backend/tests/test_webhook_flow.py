"""Acceptance gate §6.3 — webhook auth and the 60s HITL hold (spec §3, §5).

The load-bearing assertion is the last one: when the Redis TTL elapses, the
setup is gone and confirming it produces **zero broker interaction**.
"""

from __future__ import annotations

import json

import pytest

from app.core import redis_client
from app.core.security import compute_webhook_hmac

WEBHOOK_URL = "/api/v1/webhooks/tradingview"
HMAC_SECRET = "test_hmac_secret"


def signed_headers(body: bytes) -> dict:
    return {
        "X-Atlas-Signature": compute_webhook_hmac(body, HMAC_SECRET),
        "Content-Type": "application/json",
    }


def post_signal(client, payload: dict, *, sign: bool = True, signature: str | None = None):
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-Atlas-Signature"] = signature
    elif sign:
        headers.update(signed_headers(body))
    return client.post(WEBHOOK_URL, content=body, headers=headers)


class TestTransportAuth:
    def test_valid_hmac_is_accepted(self, client, signal_payload) -> None:
        response = post_signal(client, signal_payload)
        assert response.status_code == 200
        assert response.json()["status"] == "PENDING_APPROVAL"

    def test_tampered_body_is_rejected(self, client, signal_payload) -> None:
        """Signature computed over the original bytes, body then altered."""
        original = json.dumps(signal_payload).encode()
        headers = signed_headers(original)
        tampered = json.dumps({**signal_payload, "limit_price": 1.0}).encode()
        response = client.post(WEBHOOK_URL, content=tampered, headers=headers)
        assert response.status_code == 401

    def test_garbage_signature_is_rejected(self, client, signal_payload) -> None:
        assert post_signal(client, signal_payload, signature="sha256=deadbeef").status_code == 401

    def test_bad_body_token_is_rejected_when_unsigned(self, client, signal_payload) -> None:
        bad = {**signal_payload, "token": "wrong_token"}
        assert post_signal(client, bad, sign=False).status_code == 401

    def test_body_token_alone_is_accepted(self, client, signal_payload) -> None:
        """Pine's in-body token is a valid fallback when no header is sent."""
        assert post_signal(client, signal_payload, sign=False).status_code == 200


class TestPayloadValidation:
    def test_malformed_json_is_422_or_401(self, client) -> None:
        response = client.post(
            WEBHOOK_URL, content=b"not json", headers={"Content-Type": "application/json"}
        )
        assert response.status_code in (401, 422)

    @pytest.mark.parametrize("field", ["symbol", "venue", "action", "limit_price", "stop_loss"])
    def test_missing_required_field_is_422(self, client, signal_payload, field) -> None:
        incomplete = {k: v for k, v in signal_payload.items() if k != field}
        assert post_signal(client, incomplete).status_code == 422

    @pytest.mark.parametrize(
        "field,value",
        [("venue", "BINANCE"), ("action", "HODL"), ("regime", "SIDEWAYS")],
    )
    def test_enum_violations_are_422(self, client, signal_payload, field, value) -> None:
        assert post_signal(client, {**signal_payload, field: value}).status_code == 422


class TestPendingHold:
    def test_setup_is_held_in_redis_with_a_ttl(self, client, fake_redis, signal_payload) -> None:
        signal_id = post_signal(client, signal_payload).json()["signal_id"]
        assert fake_redis._store[redis_client.setup_key(signal_id)][1] is not None

    def test_ack_reports_the_60_second_window(self, client, signal_payload) -> None:
        assert post_signal(client, signal_payload).json()["expires_in"] == 60

    def test_pending_setup_is_listed(self, client, signal_payload) -> None:
        signal_id = post_signal(client, signal_payload).json()["signal_id"]
        listed = client.get("/api/v1/confirmation/pending").json()
        assert any(s["signal_id"] == signal_id for s in listed)

    def test_setup_is_retrievable_before_expiry(self, client, signal_payload) -> None:
        signal_id = post_signal(client, signal_payload).json()["signal_id"]
        detail = client.get(f"/api/v1/confirmation/{signal_id}")
        assert detail.status_code == 200
        assert detail.json()["symbol"] == "NQ1!"


class TestExpiryCausesZeroBrokerInteraction:
    """Spec §5.4: 'If user taps Reject or 60s expires → Zero broker interaction.'"""

    def test_expired_setup_confirm_is_410_and_never_reaches_a_broker(
        self, client, fake_redis, spy_brokers, signal_payload
    ) -> None:
        signal_id = post_signal(client, signal_payload).json()["signal_id"]

        # Fast-forward past the TTL without sleeping 60 seconds.
        fake_redis.expire_now(redis_client.setup_key(signal_id))

        response = client.post(
            f"/api/v1/confirmation/{signal_id}/confirm", json={"venue": "TRADOVATE"}
        )

        assert response.status_code == 410
        assert spy_brokers["TRADOVATE"].call_count == 0, "expired setup reached the broker"
        assert spy_brokers["KRAKEN"].call_count == 0

    def test_unknown_signal_id_never_reaches_a_broker(self, client, spy_brokers) -> None:
        response = client.post(
            "/api/v1/confirmation/does-not-exist/confirm", json={"venue": "KRAKEN"}
        )
        assert response.status_code == 410
        assert spy_brokers["KRAKEN"].call_count == 0

    def test_reject_never_reaches_a_broker(
        self, client, spy_brokers, signal_payload
    ) -> None:
        signal_id = post_signal(client, signal_payload).json()["signal_id"]
        response = client.post(f"/api/v1/confirmation/{signal_id}/reject", json={})
        assert response.status_code in (200, 202)
        assert spy_brokers["TRADOVATE"].call_count == 0
        assert spy_brokers["KRAKEN"].call_count == 0


class TestConfirmedRouting:
    def test_confirm_routes_to_the_chosen_venue(
        self, client, spy_brokers, signal_payload
    ) -> None:
        signal_id = post_signal(client, signal_payload).json()["signal_id"]
        response = client.post(
            f"/api/v1/confirmation/{signal_id}/confirm", json={"venue": "TRADOVATE"}
        )
        assert response.status_code == 200
        assert len(spy_brokers["TRADOVATE"].calls_named("place_bracket_order")) == 1
        assert spy_brokers["KRAKEN"].call_count == 0

    def test_route_kraken_sends_to_kraken_only(
        self, client, spy_brokers, signal_payload
    ) -> None:
        crypto = {**signal_payload, "symbol": "XBTUSD", "venue": "KRAKEN"}
        signal_id = post_signal(client, crypto).json()["signal_id"]
        response = client.post(
            f"/api/v1/confirmation/{signal_id}/confirm", json={"venue": "KRAKEN"}
        )
        assert response.status_code == 200
        assert len(spy_brokers["KRAKEN"].calls_named("place_bracket_order")) == 1
        assert spy_brokers["TRADOVATE"].call_count == 0

    def test_double_confirm_routes_only_once(
        self, client, spy_brokers, signal_payload
    ) -> None:
        """The Redis key is consumed, so a double-tap cannot double-fill."""
        signal_id = post_signal(client, signal_payload).json()["signal_id"]
        first = client.post(
            f"/api/v1/confirmation/{signal_id}/confirm", json={"venue": "TRADOVATE"}
        )
        second = client.post(
            f"/api/v1/confirmation/{signal_id}/confirm", json={"venue": "TRADOVATE"}
        )
        assert first.status_code == 200
        assert second.status_code == 410
        assert len(spy_brokers["TRADOVATE"].calls_named("place_bracket_order")) == 1


class TestKillSwitch:
    async def test_engaged_kill_switch_locks_the_webhook(
        self, client, fake_redis, signal_payload
    ) -> None:
        await redis_client.engage_kill_switch(reason="test", actor="pytest")
        assert post_signal(client, signal_payload).status_code == 423

    async def test_kill_switch_blocks_confirmation_without_broker_calls(
        self, client, fake_redis, spy_brokers, signal_payload
    ) -> None:
        signal_id = post_signal(client, signal_payload).json()["signal_id"]
        await redis_client.engage_kill_switch(reason="test", actor="pytest")
        response = client.post(
            f"/api/v1/confirmation/{signal_id}/confirm", json={"venue": "TRADOVATE"}
        )
        assert response.status_code == 423
        assert spy_brokers["TRADOVATE"].call_count == 0
