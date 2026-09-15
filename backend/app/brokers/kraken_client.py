"""Kraken margin bridge — HMAC-SHA512 request signing (spec §4.2).

Every private POST is authenticated with::

    API-Key:  <KRAKEN_API_KEY>
    API-Sign: base64(HMAC-SHA512(urlpath || SHA256(nonce || urlencode(data)),
                                 base64decode(secret)))

Only ``hmac``/``hashlib``/``base64``/``urllib`` are used — the ``cryptography``
package is deliberately never imported.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from typing import Any, Dict, Optional

from app.brokers.base import BrokerClient, BrokerError
from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

ADD_ORDER_PATH = "/0/private/AddOrder"
CANCEL_ALL_PATH = "/0/private/CancelAll"
OPEN_POSITIONS_PATH = "/0/private/OpenPositions"
BALANCE_PATH = "/0/private/Balance"


# ── Signature (spec §4.2, byte-for-byte) ───────────────────────────────────
def kraken_sign(urlpath: str, data: Dict[str, Any], secret: str) -> str:
    """Reproduce the canonical Kraken API reference signature exactly.

    The ``data`` mapping's **insertion order is significant** because
    ``urllib.parse.urlencode`` preserves it and the digest covers the encoded
    string verbatim.
    """
    postdata = urllib.parse.urlencode(data)
    encoded = (str(data["nonce"]) + postdata).encode()
    message = urlpath.encode() + hashlib.sha256(encoded).digest()
    mac = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


def generate_nonce() -> int:
    """Millisecond nonce — must increase monotonically per API key."""
    return int(time.time() * 1000)


def _mock_txid(payload: Dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(blob).hexdigest().upper()
    return f"O{digest[:6]}-{digest[6:11]}-{digest[11:17]}"


class KrakenClient(BrokerClient):
    """Async Kraken margin REST bridge."""

    venue = "KRAKEN"

    def __init__(self, settings: Optional[Settings] = None, http_client: Any = None) -> None:
        self.settings = settings or get_settings()
        self.base_url = self.settings.kraken_base_url.rstrip("/")
        self._http = http_client
        self._owns_http = http_client is None

    async def _client(self) -> Any:
        if self._http is None:
            import httpx

            self._http = httpx.AsyncClient(timeout=15.0)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and self._owns_http:
            try:
                await self._http.aclose()
            except Exception:  # pragma: no cover
                pass
        self._http = None

    # ── payload construction (FROZEN, spec §4.2) ───────────────────────────
    def build_add_order_payload(
        self,
        pair: str,
        action: str,
        volume: float,
        price: float,
        sl: float,
        leverage: int = 3,
        nonce: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Exactly the spec §4.2 ``AddOrder`` body.

        Key insertion order matters for the signature, so it is fixed here.
        """
        return {
            "nonce": generate_nonce() if nonce is None else nonce,
            "ordertype": "limit",
            "type": str(action).lower(),
            "pair": pair,
            "price": str(price),
            "volume": str(volume),
            "leverage": str(leverage),
            "oflags": "fcib",  # fee currency in base
            "close[ordertype]": "stop-loss",
            "close[price]": str(sl),
        }

    def sign_headers(self, urlpath: str, data: Dict[str, Any]) -> Dict[str, str]:
        """``API-Key`` / ``API-Sign`` headers for one private POST."""
        secret = self.settings.kraken_api_secret or _MOCK_SECRET
        return {
            "API-Key": self.settings.kraken_api_key or "MOCK_API_KEY",
            "API-Sign": kraken_sign(urlpath, data, secret),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }

    async def _private_post(self, urlpath: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Sign then POST; short-circuits the network in mock mode."""
        headers = self.sign_headers(urlpath, data)
        if self.settings.is_mock:
            logger.info("MOCK kraken %s %s", urlpath, data.get("pair", ""))
            return {
                "mock": True,
                "urlpath": urlpath,
                "headers": headers,
                "payload": data,
                "error": [],
            }
        client = await self._client()
        response = await client.post(
            f"{self.base_url}{urlpath}",
            data=urllib.parse.urlencode(data),
            headers=headers,
        )
        status = getattr(response, "status_code", 200)
        try:
            body = response.json()
        except Exception as exc:
            raise BrokerError(self.venue, f"{urlpath}: non-JSON response (HTTP {status})") from exc
        if status >= 400:
            raise BrokerError(self.venue, f"{urlpath}: HTTP {status}", body if isinstance(body, dict) else {})
        if isinstance(body, dict) and body.get("error"):
            raise BrokerError(self.venue, "; ".join(map(str, body["error"])), body)
        if isinstance(body, dict):
            body["mock"] = False
            body["payload"] = data
            return body
        return {"result": body, "mock": False, "payload": data}

    # ── order routing ──────────────────────────────────────────────────────
    async def place_bracket_order(
        self,
        *,
        symbol: str,
        action: str,
        qty: float,
        limit_price: float,
        stop_loss: float,
        take_profit: float | None = None,
        leverage: Optional[int] = None,
        nonce: Optional[int] = None,
        **_: Any,
    ) -> Dict[str, Any]:
        """Leveraged limit entry with an exchange-side conditional stop-loss close.

        Kraken conditional closes carry a single leg, so ``take_profit`` is
        recorded for the audit ledger but not sent to the venue.
        """
        payload = self.build_add_order_payload(
            pair=symbol,
            action=action,
            volume=qty,
            price=limit_price,
            sl=stop_loss,
            leverage=leverage if leverage is not None else self.settings.kraken_default_leverage,
            nonce=nonce,
        )
        response = await self._private_post(ADD_ORDER_PATH, payload)
        if response.get("mock"):
            response["result"] = {
                "descr": {
                    "order": (
                        f"{payload['type']} {payload['volume']} {payload['pair']} @ limit "
                        f"{payload['price']} with {payload['leverage']}:1 leverage"
                    )
                },
                "txid": [_mock_txid(payload)],
            }
        response["take_profit"] = take_profit
        return response

    # ── emergency surface ──────────────────────────────────────────────────
    async def cancel_all(self, **_: Any) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"nonce": generate_nonce()}
        response = await self._private_post(CANCEL_ALL_PATH, payload)
        if response.get("mock"):
            response["result"] = {"count": 0}
        response["venue"] = self.venue
        return response

    async def flatten_all(self, **_: Any) -> Dict[str, Any]:
        """Close every margin position by submitting opposing reduce orders.

        In mock mode nothing is sent; live mode reads ``OpenPositions`` and
        submits a market close per position with ``reduce_only``.
        """
        positions = await self.get_open_positions()
        closed: list[Dict[str, Any]] = []
        if self.settings.is_mock:
            return {"venue": self.venue, "mock": True, "flattened": 0, "positions": positions}
        for txid, position in (positions.get("result") or {}).items():
            side = str(position.get("type", "buy")).lower()
            opposite = "sell" if side == "buy" else "buy"
            payload = {
                "nonce": generate_nonce(),
                "ordertype": "market",
                "type": opposite,
                "pair": position.get("pair", ""),
                "volume": str(position.get("vol", "0")),
                "leverage": str(self.settings.kraken_default_leverage),
                "reduce_only": "true",
            }
            closed.append({"txid": txid, "response": await self._private_post(ADD_ORDER_PATH, payload)})
        return {"venue": self.venue, "mock": False, "flattened": len(closed), "closed": closed}

    async def get_open_positions(self) -> Dict[str, Any]:
        payload = {"nonce": generate_nonce(), "docalcs": "true"}
        response = await self._private_post(OPEN_POSITIONS_PATH, payload)
        if response.get("mock"):
            response["result"] = {}
        return response

    async def get_account(self, **_: Any) -> Dict[str, Any]:
        payload = {"nonce": generate_nonce()}
        response = await self._private_post(BALANCE_PATH, payload)
        if response.get("mock"):
            response["result"] = {"ZUSD": f"{self.settings.account_equity:.4f}"}
        response["venue"] = self.venue
        return response


#: A syntactically valid base64 secret so mock-mode signing still exercises the
#: real code path when ``KRAKEN_API_SECRET`` is empty.
_MOCK_SECRET = base64.b64encode(b"atlas-mock-kraken-secret-000000000000").decode()
