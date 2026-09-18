"""Acceptance gate §6.2 — Kraken HMAC-SHA512 signing (spec §4.2).

The decisive assertion is the *canonical Kraken API reference vector*: if
``kraken_sign`` reproduces it byte-for-byte, the digest construction
(``urlencode`` → ``SHA256(nonce||postdata)`` → ``HMAC-SHA512(urlpath||digest)``
→ base64) is provably correct against the venue's own published example.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import urllib.parse

import pytest

from app.brokers.kraken_client import (
    ADD_ORDER_PATH,
    KrakenClient,
    generate_nonce,
    kraken_sign,
)

# ── Canonical vector published by Kraken for API-Sign ──────────────────────
REF_SECRET = (
    "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5"
    "nE9qa99HAZtuZuj6F1huXg=="
)
REF_URLPATH = "/0/private/AddOrder"
REF_DATA = {
    "nonce": "1616492376594",
    "ordertype": "limit",
    "pair": "XBTUSD",
    "price": "37500",
    "type": "buy",
    "volume": "1.25",
}
REF_SIGNATURE = (
    "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32"
    "bAb0nmbRn6H8ndwLUQ=="
)


class TestCanonicalVector:
    def test_reference_signature_matches_byte_for_byte(self) -> None:
        assert kraken_sign(REF_URLPATH, dict(REF_DATA), REF_SECRET) == REF_SIGNATURE

    def test_signature_is_valid_base64_of_a_sha512_digest(self) -> None:
        raw = base64.b64decode(kraken_sign(REF_URLPATH, dict(REF_DATA), REF_SECRET))
        assert len(raw) == 64  # SHA-512 → 64 bytes

    def test_algorithm_is_reconstructible_from_the_spec_text(self) -> None:
        """Independently re-derive the digest exactly as spec §4.2 states."""
        postdata = urllib.parse.urlencode(REF_DATA)
        encoded = (str(REF_DATA["nonce"]) + postdata).encode()
        message = REF_URLPATH.encode() + hashlib.sha256(encoded).digest()
        mac = hmac.new(base64.b64decode(REF_SECRET), message, hashlib.sha512)
        assert base64.b64encode(mac.digest()).decode() == REF_SIGNATURE

    def test_urlpath_is_bound_into_the_digest(self) -> None:
        other = kraken_sign("/0/private/CancelAll", dict(REF_DATA), REF_SECRET)
        assert other != REF_SIGNATURE

    def test_nonce_is_bound_into_the_digest(self) -> None:
        tampered = dict(REF_DATA, nonce="1616492376595")
        assert kraken_sign(REF_URLPATH, tampered, REF_SECRET) != REF_SIGNATURE

    def test_secret_is_bound_into_the_digest(self) -> None:
        other_secret = base64.b64encode(b"a-different-secret-0000000000000000").decode()
        assert kraken_sign(REF_URLPATH, dict(REF_DATA), other_secret) != REF_SIGNATURE

    def test_key_order_is_significant(self) -> None:
        """urlencode preserves insertion order, so a reordering changes the sig."""
        reordered = {k: REF_DATA[k] for k in reversed(list(REF_DATA))}
        assert kraken_sign(REF_URLPATH, reordered, REF_SECRET) != REF_SIGNATURE


class TestAddOrderPayload:
    @pytest.fixture
    def payload(self) -> dict:
        return KrakenClient().build_add_order_payload(
            pair="XBTUSD",
            action="BUY",
            volume=1.25,
            price=37500.0,
            sl=36800.0,
            leverage=3,
            nonce=1616492376594,
        )

    def test_carries_every_spec_mandated_key(self, payload: dict) -> None:
        for key in (
            "nonce", "ordertype", "type", "pair", "price", "volume",
            "leverage", "oflags", "close[ordertype]", "close[price]",
        ):
            assert key in payload, f"missing spec §4.2 key {key!r}"

    def test_margin_and_conditional_close_fields(self, payload: dict) -> None:
        assert payload["ordertype"] == "limit"
        assert payload["leverage"] == "3"              # leveraged margin add
        assert payload["oflags"] == "fcib"             # fee currency in base
        assert payload["close[ordertype]"] == "stop-loss"
        assert payload["close[price]"] == "36800.0"

    def test_action_is_lowercased_for_kraken(self, payload: dict) -> None:
        assert payload["type"] == "buy"
        sell = KrakenClient().build_add_order_payload(
            pair="XBTUSD", action="SELL", volume=1.0, price=1.0, sl=2.0
        )
        assert sell["type"] == "sell"

    def test_numeric_fields_are_stringified(self, payload: dict) -> None:
        for key in ("price", "volume", "leverage", "close[price]"):
            assert isinstance(payload[key], str)

    def test_payload_signs_without_error(self, payload: dict) -> None:
        assert len(kraken_sign(ADD_ORDER_PATH, payload, REF_SECRET)) > 0


class TestSigningHeaders:
    def test_private_post_sets_api_key_and_api_sign(self) -> None:
        client = KrakenClient()
        data = client.build_add_order_payload(
            pair="XBTUSD", action="BUY", volume=1.0, price=37500.0, sl=36800.0
        )
        headers = client.sign_headers(ADD_ORDER_PATH, data)
        assert "API-Key" in headers and "API-Sign" in headers
        assert headers["Content-Type"] == "application/x-www-form-urlencoded"
        assert base64.b64decode(headers["API-Sign"])  # decodes cleanly

    def test_nonce_is_monotonic(self) -> None:
        assert generate_nonce() <= generate_nonce()


class TestMockModeStillSigns:
    async def test_mock_order_signs_and_skips_the_network(self) -> None:
        """Mock mode must exercise the real signing path, not bypass it."""
        client = KrakenClient()
        result = await client.place_bracket_order(
            symbol="XBTUSD", action="BUY", qty=1.25,
            limit_price=37500.0, stop_loss=36800.0, take_profit=39000.0,
            nonce=1616492376594,
        )
        assert result["mock"] is True
        assert result["payload"]["close[price]"] == "36800.0"
        assert base64.b64decode(result["headers"]["API-Sign"])
        assert result["error"] == []
