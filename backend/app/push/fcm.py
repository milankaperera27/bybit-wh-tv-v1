"""Firebase Cloud Messaging HTTP v1 — high-priority actionable push (spec §5).

The notification carries three action buttons:

    [ Confirm Tradovate ]  [ Route Kraken ]  [ Reject ]

and always ships ``signal_id`` in the ``data`` block so the PWA can POST back to
``/api/v1/confirmation/{signal_id}/…``.

Access tokens come from a service-account JWT assertion.  RS256 signing needs an
RSA implementation, which the standard library does not provide, so
``google-auth`` is imported **lazily inside the function** — the module (and the
whole test-suite) imports fine without it.  ``cryptography`` is never imported by
this module at any point.

Failure is never fatal: every path degrades to a logged mock message id so a
push outage can never abort a webhook.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, Mapping, Optional

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM_ENDPOINT_TEMPLATE = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"

#: Android notification action ids — mirrored by the PWA service worker.
ACTION_CONFIRM_TRADOVATE = "CONFIRM_TRADOVATE"
ACTION_ROUTE_KRAKEN = "ROUTE_KRAKEN"
ACTION_REJECT = "REJECT"

NOTIFICATION_ACTIONS = [
    {"action": ACTION_CONFIRM_TRADOVATE, "title": "Confirm Tradovate"},
    {"action": ACTION_ROUTE_KRAKEN, "title": "Route Kraken"},
    {"action": ACTION_REJECT, "title": "Reject"},
]

#: Android notification channel the PWA registers at install time.
ANDROID_CHANNEL_ID = "atlas_setups"

_token_cache: Dict[str, Any] = {"token": None, "expires_at": 0.0}


class FCMUnavailable(RuntimeError):
    """Raised internally when credentials are missing; always caught."""


def _mock_message_id(payload: Mapping[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return f"projects/mock/messages/{hashlib.sha256(blob).hexdigest()[:24]}"


def _as_dict(setup: Any) -> Dict[str, Any]:
    """Accept a :class:`PendingSetup`, a plain dict, or anything dict-able."""
    if isinstance(setup, Mapping):
        return dict(setup)
    dump = getattr(setup, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return dict(getattr(setup, "__dict__", {}) or {})


def credentials_available(settings: Optional[Settings] = None) -> bool:
    """True only when a project id and a readable service-account file exist."""
    cfg = settings or get_settings()
    if not cfg.fcm_project_id:
        return False
    path = cfg.fcm_service_account_json
    return bool(path) and os.path.isfile(path)


async def get_access_token(settings: Optional[Settings] = None) -> str:
    """OAuth2 bearer for FCM HTTP v1, cached until ~5 minutes before expiry.

    ``google.oauth2.service_account`` is imported here (not at module scope) so
    the dependency stays optional.
    """
    cfg = settings or get_settings()
    now = time.time()
    if _token_cache["token"] and now < float(_token_cache["expires_at"]):
        return str(_token_cache["token"])

    if not credentials_available(cfg):
        raise FCMUnavailable("FCM project id or service-account JSON is missing")

    try:
        from google.auth.transport.requests import Request  # type: ignore
        from google.oauth2 import service_account  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise FCMUnavailable(
            "google-auth is not installed; FCM HTTP v1 requires it for the RS256 "
            "JWT assertion"
        ) from exc

    def _refresh() -> tuple[str, float]:
        creds = service_account.Credentials.from_service_account_file(
            cfg.fcm_service_account_json, scopes=[FCM_SCOPE]
        )
        creds.refresh(Request())
        expiry = creds.expiry.timestamp() if creds.expiry else (time.time() + 3000)
        return creds.token, expiry

    import anyio

    token, expiry = await anyio.to_thread.run_sync(_refresh)
    _token_cache["token"] = token
    _token_cache["expires_at"] = expiry - 300
    return str(token)


def build_message(
    setup: Any,
    device_token: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> Dict[str, Any]:
    """Assemble the FCM HTTP v1 ``message`` envelope for one pending setup."""
    cfg = settings or get_settings()
    data = _as_dict(setup)

    signal_id = str(data.get("signal_id", ""))
    symbol = str(data.get("symbol", "?"))
    action = str(data.get("action", "?"))
    action = getattr(action, "value", action)
    venue = str(data.get("venue", "?"))
    venue = getattr(venue, "value", venue)
    regime = str(data.get("regime", "?"))
    regime = getattr(regime, "value", regime)
    qty = data.get("qty", 0)
    limit_price = data.get("limit_price", 0)
    stop_loss = data.get("stop_loss", 0)
    take_profit = data.get("take_profit_1", 0)
    risk_amount = data.get("risk_amount", 0)
    ttl = int(data.get("ttl_seconds", cfg.confirmation_ttl_seconds) or cfg.confirmation_ttl_seconds)

    title = f"{action} {symbol} — {qty} @ {limit_price}"
    body = (
        f"{venue} · {regime}\n"
        f"SL {stop_loss} · TP {take_profit} · risk {risk_amount}\n"
        f"Expires in {ttl}s"
    )

    message: Dict[str, Any] = {
        "token": device_token or cfg.fcm_default_device_token,
        "notification": {"title": title, "body": body},
        "data": {
            # Every value in `data` MUST be a string per the FCM v1 schema.
            "signal_id": signal_id,
            "symbol": symbol,
            "venue": venue,
            "action": action,
            "regime": regime,
            "qty": str(qty),
            "limit_price": str(limit_price),
            "stop_loss": str(stop_loss),
            "take_profit_1": str(take_profit),
            "risk_amount": str(risk_amount),
            "ttl_seconds": str(ttl),
            "click_action": "ATLAS_PENDING_SETUP",
            "actions": json.dumps(NOTIFICATION_ACTIONS),
        },
        "android": {
            "priority": "HIGH",
            "ttl": f"{ttl}s",
            "collapse_key": f"atlas_setup_{signal_id}",
            "notification": {
                "channel_id": ANDROID_CHANNEL_ID,
                "click_action": "ATLAS_PENDING_SETUP",
                "notification_priority": "PRIORITY_MAX",
                "default_sound": True,
                "default_vibrate_timings": True,
                "visibility": "PUBLIC",
                "tag": f"atlas_setup_{signal_id}",
            },
        },
        "webpush": {
            "headers": {"Urgency": "high", "TTL": str(ttl)},
            "notification": {
                "title": title,
                "body": body,
                "requireInteraction": True,
                "tag": f"atlas_setup_{signal_id}",
                "actions": NOTIFICATION_ACTIONS,
            },
            "fcm_options": {"link": f"/setup/{signal_id}"},
        },
        "apns": {
            "headers": {"apns-priority": "10"},
            "payload": {"aps": {"category": "ATLAS_PENDING_SETUP", "sound": "default"}},
        },
    }
    return message


async def send_actionable_setup(
    setup: Any,
    device_token: Optional[str] = None,
    settings: Optional[Settings] = None,
    http_client: Any = None,
) -> Dict[str, Any]:
    """Send the 1-tap confirmation push.  **Never raises.**

    Returns ``{"message_id": ..., "mock": bool, "message": <envelope>}``.
    """
    cfg = settings or get_settings()
    message = build_message(setup, device_token=device_token, settings=cfg)
    envelope = {"message": message}

    target = message.get("token")
    if cfg.is_mock or not credentials_available(cfg) or not target:
        reason = (
            "mock-mode"
            if cfg.is_mock
            else ("no-device-token" if not target else "no-fcm-credentials")
        )
        logger.info("FCM push short-circuited (%s) for signal %s", reason, message["data"]["signal_id"])
        return {
            "message_id": _mock_message_id(envelope),
            "mock": True,
            "reason": reason,
            "message": message,
        }

    try:
        token = await get_access_token(cfg)
        client = http_client
        owns = client is None
        if owns:
            import httpx

            client = httpx.AsyncClient(timeout=10.0)
        try:
            response = await client.post(
                FCM_ENDPOINT_TEMPLATE.format(project_id=cfg.fcm_project_id),
                json=envelope,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; UTF-8",
                },
            )
            status = getattr(response, "status_code", 200)
            body = response.json() if hasattr(response, "json") else {}
            if status >= 400:
                logger.warning("FCM rejected the push (HTTP %s): %s", status, body)
                return {
                    "message_id": _mock_message_id(envelope),
                    "mock": True,
                    "reason": f"fcm-http-{status}",
                    "error": body,
                    "message": message,
                }
            return {
                "message_id": body.get("name") if isinstance(body, dict) else None,
                "mock": False,
                "message": message,
            }
        finally:
            if owns and client is not None:
                await client.aclose()
    except Exception as exc:  # noqa: BLE001 — a push failure must never abort a webhook
        logger.warning("FCM push failed, degrading to mock: %s", exc, exc_info=True)
        return {
            "message_id": _mock_message_id(envelope),
            "mock": True,
            "reason": "exception",
            "error": str(exc),
            "message": message,
        }


def reset_token_cache() -> None:
    """Drop the cached OAuth2 token (tests / credential rotation)."""
    _token_cache["token"] = None
    _token_cache["expires_at"] = 0.0
