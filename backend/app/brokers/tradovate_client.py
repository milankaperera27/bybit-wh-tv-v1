"""Tradovate CME bridge — OAuth2 bearer + automated bracket orders (spec §4.1).

Endpoints (``{base}`` = ``TRADOVATE_BASE_URL``, e.g.
``https://demo.tradovateapi.com/v1``):

* ``POST {base}/auth/accessTokenRequest``  — name/password/appId/appVersion/cid/sec/deviceId
* ``POST {base}/auth/renewAccessToken``    — refresh before ``expirationTime``
* ``POST {base}/order/placeorder``         — the bracket submission

In mock mode the payload is still built and the Bearer header still assembled;
only ``httpx`` is skipped, and a deterministic mock response is returned.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from app.brokers.base import BrokerClient, BrokerError
from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

#: Refresh the session this far ahead of the advertised expiry.
TOKEN_REFRESH_MARGIN_SECONDS = 120


def _mock_order_id(payload: Dict[str, Any]) -> int:
    """Stable pseudo order id derived from the payload itself."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return int.from_bytes(hashlib.sha256(blob).digest()[:6], "big") % 1_000_000_000


class TradovateClient(BrokerClient):
    """Async Tradovate REST bridge."""

    venue = "TRADOVATE"

    def __init__(
        self,
        settings: Optional[Settings] = None,
        http_client: Any = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.base_url = self.settings.tradovate_base_url.rstrip("/")
        self._http = http_client
        self._owns_http = http_client is None
        self._access_token: Optional[str] = None
        self._md_access_token: Optional[str] = None
        self._expires_at: Optional[datetime] = None
        self._user_id: Optional[int] = None

    # ── transport ──────────────────────────────────────────────────────────
    async def _client(self) -> Any:
        if self._http is None:
            import httpx

            self._http = httpx.AsyncClient(timeout=15.0)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and self._owns_http:
            try:
                await self._http.aclose()
            except Exception:  # pragma: no cover - shutdown best effort
                pass
        self._http = None

    # ── OAuth2 session management ──────────────────────────────────────────
    def build_auth_payload(self) -> Dict[str, Any]:
        """The exact ``/auth/accessTokenRequest`` body Tradovate expects."""
        cfg = self.settings
        return {
            "name": cfg.tradovate_username,
            "password": cfg.tradovate_password,
            "appId": cfg.tradovate_app_id,
            "appVersion": cfg.tradovate_app_version,
            "cid": cfg.tradovate_cid,
            "sec": cfg.tradovate_secret,
            "deviceId": cfg.tradovate_device_id,
        }

    @property
    def token_is_valid(self) -> bool:
        if not self._access_token or self._expires_at is None:
            return False
        margin = timedelta(seconds=TOKEN_REFRESH_MARGIN_SECONDS)
        return datetime.now(timezone.utc) + margin < self._expires_at

    def _absorb_token(self, body: Dict[str, Any]) -> str:
        token = body.get("accessToken")
        if not token:
            raise BrokerError(self.venue, f"auth response carried no accessToken: {body}")
        self._access_token = token
        self._md_access_token = body.get("mdAccessToken")
        self._user_id = body.get("userId")
        self._expires_at = _parse_expiration(body.get("expirationTime"))
        return token

    async def authenticate(self, force: bool = False) -> str:
        """Obtain (or renew) the bearer token, caching until ``expirationTime``."""
        if self.settings.is_mock:
            self._access_token = "MOCK_ACCESS_TOKEN"
            self._expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
            return self._access_token

        if not force and self.token_is_valid:
            return self._access_token  # type: ignore[return-value]

        # An expired-but-present token can be renewed cheaply.
        if not force and self._access_token:
            try:
                return await self.renew_access_token()
            except BrokerError:
                logger.info("tradovate renew failed; falling back to full auth")

        client = await self._client()
        response = await client.post(
            f"{self.base_url}/auth/accessTokenRequest",
            json=self.build_auth_payload(),
            headers={"Content-Type": "application/json"},
        )
        body = _json_or_raise(response, self.venue, "accessTokenRequest")
        if body.get("errorText"):
            raise BrokerError(self.venue, str(body["errorText"]), body)
        return self._absorb_token(body)

    async def renew_access_token(self) -> str:
        """``POST /auth/renewAccessToken`` using the current bearer."""
        if self.settings.is_mock:
            return await self.authenticate()
        if not self._access_token:
            raise BrokerError(self.venue, "no cached token to renew")
        client = await self._client()
        response = await client.post(
            f"{self.base_url}/auth/renewAccessToken",
            headers=self._auth_headers(self._access_token),
        )
        body = _json_or_raise(response, self.venue, "renewAccessToken")
        if body.get("errorText"):
            raise BrokerError(self.venue, str(body["errorText"]), body)
        return self._absorb_token(body)

    @staticmethod
    def _auth_headers(token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def auth_headers(self, token: Optional[str] = None) -> Dict[str, str]:
        """Public accessor used by the acceptance test (artifact §6.1)."""
        return self._auth_headers(token or self._access_token or "MOCK_ACCESS_TOKEN")

    # ── payload construction (FROZEN, spec §4.1) ───────────────────────────
    def build_bracket_payload(
        self,
        *,
        symbol: str,
        action: str,
        qty: float,
        limit_price: float,
        stop_loss: float,
        take_profit: float,
        account_spec: Optional[str] = None,
        account_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Exactly the spec §4.1 shape — key set and casing are frozen."""
        return {
            "accountSpec": account_spec or self.settings.tradovate_account_spec,
            "accountId": int(account_id if account_id is not None else self.settings.tradovate_account_id),
            "action": str(action).capitalize(),
            "symbol": symbol,
            "orderQty": int(qty),
            "orderType": "Limit",
            "price": float(limit_price),
            "isAutomated": True,
            "bracket": {
                "stopLoss": float(stop_loss),
                "takeProfit": float(take_profit),
            },
        }

    # ── order routing ──────────────────────────────────────────────────────
    async def place_bracket_order(
        self,
        *,
        symbol: str,
        action: str,
        qty: float,
        limit_price: float,
        stop_loss: float,
        take_profit: float,
        account_spec: Optional[str] = None,
        account_id: Optional[int] = None,
        **_: Any,
    ) -> Dict[str, Any]:
        """``POST {base}/order/placeorder`` with a Bearer header."""
        token = await self.authenticate()
        payload = self.build_bracket_payload(
            symbol=symbol,
            action=action,
            qty=qty,
            limit_price=limit_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            account_spec=account_spec,
            account_id=account_id,
        )
        headers = self._auth_headers(token)

        if self.settings.is_mock:
            logger.info("MOCK tradovate placeorder %s %s x%s", action, symbol, qty)
            return {"orderId": _mock_order_id(payload), "mock": True, "payload": payload}

        client = await self._client()
        response = await client.post(
            f"{self.base_url}/order/placeorder", json=payload, headers=headers
        )
        body = _json_or_raise(response, self.venue, "placeorder")
        if body.get("failureReason") or body.get("errorText"):
            raise BrokerError(
                self.venue,
                str(body.get("failureText") or body.get("errorText") or body.get("failureReason")),
                body,
            )
        body.setdefault("mock", False)
        body["payload"] = payload
        return body

    # ── emergency surface ──────────────────────────────────────────────────
    async def cancel_all(self, **_: Any) -> Dict[str, Any]:
        """Liquidate working orders via ``/order/liquidateposition`` semantics."""
        token = await self.authenticate()
        account_id = int(self.settings.tradovate_account_id)
        payload = {"accountId": account_id}
        if self.settings.is_mock:
            return {"venue": self.venue, "cancelled": 0, "mock": True, "payload": payload}
        client = await self._client()
        response = await client.post(
            f"{self.base_url}/order/cancelallorders",
            json=payload,
            headers=self._auth_headers(token),
        )
        return {
            "venue": self.venue,
            "mock": False,
            "response": _json_or_raise(response, self.venue, "cancelallorders"),
        }

    async def flatten_all(self, **_: Any) -> Dict[str, Any]:
        """``/order/liquidateposition`` for every open position."""
        token = await self.authenticate()
        account_id = int(self.settings.tradovate_account_id)
        payload = {"accountId": account_id, "admin": False}
        if self.settings.is_mock:
            return {"venue": self.venue, "flattened": 0, "mock": True, "payload": payload}
        client = await self._client()
        response = await client.post(
            f"{self.base_url}/order/liquidateposition",
            json=payload,
            headers=self._auth_headers(token),
        )
        return {
            "venue": self.venue,
            "mock": False,
            "response": _json_or_raise(response, self.venue, "liquidateposition"),
        }

    async def get_account(self, **_: Any) -> Dict[str, Any]:
        token = await self.authenticate()
        if self.settings.is_mock:
            return {
                "venue": self.venue,
                "mock": True,
                "accountSpec": self.settings.tradovate_account_spec,
                "accountId": int(self.settings.tradovate_account_id),
                "equity": self.settings.account_equity,
                "openPositions": 0,
            }
        client = await self._client()
        response = await client.get(
            f"{self.base_url}/account/list", headers=self._auth_headers(token)
        )
        return {
            "venue": self.venue,
            "mock": False,
            "accounts": _json_or_raise(response, self.venue, "account/list"),
        }


# ── helpers ────────────────────────────────────────────────────────────────
def _parse_expiration(value: Any) -> datetime:
    """Tradovate returns ISO-8601 with a trailing ``Z``."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc) + timedelta(minutes=30)


def _json_or_raise(response: Any, venue: str, label: str) -> Dict[str, Any]:
    status = getattr(response, "status_code", 200)
    try:
        body = response.json()
    except Exception as exc:  # pragma: no cover - malformed upstream
        raise BrokerError(venue, f"{label}: non-JSON response (HTTP {status})") from exc
    if status >= 400:
        raise BrokerError(venue, f"{label}: HTTP {status}", body if isinstance(body, dict) else {})
    if not isinstance(body, dict):
        return {"result": body}
    return body
