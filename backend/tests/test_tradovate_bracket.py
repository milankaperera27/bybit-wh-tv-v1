"""Acceptance gate §6.1 — Tradovate CME bracket payload (spec §4.1).

Asserts the submitted body matches the spec's reference snippet key-for-key
and that the OAuth2 Bearer header is assembled, including in mock mode.
"""

from __future__ import annotations

import pytest

from app.brokers.tradovate_client import TradovateClient

SPEC_KEYS = {
    "accountSpec", "accountId", "action", "symbol",
    "orderQty", "orderType", "price", "isAutomated", "bracket",
}


@pytest.fixture
def client() -> TradovateClient:
    return TradovateClient()


@pytest.fixture
def payload(client: TradovateClient) -> dict:
    return client.build_bracket_payload(
        symbol="NQZ5",
        action="BUY",
        qty=2,
        limit_price=20155.25,
        stop_loss=20140.00,
        take_profit=20178.12,
    )


class TestBracketPayloadShape:
    def test_exact_spec_key_set(self, payload: dict) -> None:
        assert set(payload) == SPEC_KEYS

    def test_account_routing_fields(self, payload: dict) -> None:
        assert payload["accountSpec"] == "ACCOUNT_DEMO"
        assert payload["accountId"] == 12345
        assert isinstance(payload["accountId"], int)

    def test_action_is_capitalised(self, client: TradovateClient) -> None:
        """Tradovate expects 'Buy'/'Sell', not 'BUY'/'SELL'."""
        for raw in ("BUY", "buy", "Buy"):
            built = client.build_bracket_payload(
                symbol="NQZ5", action=raw, qty=1,
                limit_price=1.0, stop_loss=0.5, take_profit=2.0,
            )
            assert built["action"] == "Buy"
        sell = client.build_bracket_payload(
            symbol="NQZ5", action="SELL", qty=1,
            limit_price=1.0, stop_loss=2.0, take_profit=0.5,
        )
        assert sell["action"] == "Sell"

    def test_order_fields(self, payload: dict) -> None:
        assert payload["symbol"] == "NQZ5"
        assert payload["orderQty"] == 2
        assert isinstance(payload["orderQty"], int)
        assert payload["orderType"] == "Limit"
        assert payload["price"] == pytest.approx(20155.25)

    def test_is_automated_flag_is_true(self, payload: dict) -> None:
        """Required by Tradovate for unattended (algo) submissions."""
        assert payload["isAutomated"] is True

    def test_bracket_legs(self, payload: dict) -> None:
        bracket = payload["bracket"]
        assert set(bracket) == {"stopLoss", "takeProfit"}
        assert bracket["stopLoss"] == pytest.approx(20140.00)
        assert bracket["takeProfit"] == pytest.approx(20178.12)

    def test_bracket_is_nested_not_flattened(self, payload: dict) -> None:
        assert isinstance(payload["bracket"], dict)
        assert "stopLoss" not in payload and "takeProfit" not in payload

    def test_account_override_is_honoured(self, client: TradovateClient) -> None:
        built = client.build_bracket_payload(
            symbol="MESZ5", action="SELL", qty=4,
            limit_price=5900.0, stop_loss=5910.0, take_profit=5880.0,
            account_spec="ACCOUNT_LIVE", account_id=99,
        )
        assert built["accountSpec"] == "ACCOUNT_LIVE"
        assert built["accountId"] == 99


class TestAuthHeaders:
    def test_bearer_header_is_set(self, client: TradovateClient) -> None:
        headers = client.auth_headers("TOKEN123")
        assert headers["Authorization"] == "Bearer TOKEN123"
        assert headers["Content-Type"] == "application/json"

    async def test_mock_authenticate_yields_a_token(self, client: TradovateClient) -> None:
        assert await client.authenticate() == "MOCK_ACCESS_TOKEN"


class TestMockPlacement:
    async def test_mock_place_builds_payload_and_skips_network(
        self, client: TradovateClient
    ) -> None:
        result = await client.place_bracket_order(
            symbol="NQZ5", action="BUY", qty=2,
            limit_price=20155.25, stop_loss=20140.00, take_profit=20178.12,
        )
        assert result["mock"] is True
        assert isinstance(result["orderId"], int)
        assert set(result["payload"]) == SPEC_KEYS
        assert result["payload"]["isAutomated"] is True

    async def test_mock_order_id_is_deterministic(self, client: TradovateClient) -> None:
        kwargs = dict(
            symbol="NQZ5", action="BUY", qty=2,
            limit_price=20155.25, stop_loss=20140.00, take_profit=20178.12,
        )
        first = await client.place_bracket_order(**kwargs)
        second = await client.place_bracket_order(**kwargs)
        assert first["orderId"] == second["orderId"]
