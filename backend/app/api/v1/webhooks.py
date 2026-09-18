"""TradingView alert ingest — FROZEN contract A (artifact §3).

``POST /api/v1/webhooks/tradingview``

1. read the **raw** body (HMAC covers the bytes, not a re-serialisation)
2. authenticate: ``X-Atlas-Signature`` wins; otherwise the in-body ``token``
3. 423 immediately if the global kill switch is engaged
4. parse into :class:`TradingViewSignal` (422 on malformed)
5. run the risk engine
6. persist to the audit ledger
7. ``SETEX`` the :class:`PendingSetup` for ``ATLAS_CONFIRMATION_TTL_SECONDS``
8. fire the FCM actionable push (failures are logged, never fatal)
9. return ``{"signal_id", "status": "PENDING_APPROVAL", "expires_in"}``
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import APIRouter, Header, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.core import ledger, redis_client
from app.core.config import get_settings
from app.core.risk_engine import evaluate as evaluate_risk
from app.core.security import authenticate_webhook
from app.core.state_machine import SignalStatus
from app.models.schemas import PendingSetup, TradingViewSignal, WebhookAck
from app.push import fcm

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post(
    "/tradingview",
    response_model=WebhookAck,
    responses={
        401: {"description": "HMAC signature or body token rejected"},
        422: {"description": "Malformed alert payload"},
        423: {"description": "Global kill switch engaged"},
    },
)
async def tradingview_webhook(
    request: Request,
    response: Response,
    x_atlas_signature: Optional[str] = Header(default=None, alias="X-Atlas-Signature"),
) -> Any:
    settings = get_settings()
    raw_body = await request.body()

    # ── 1/2. transport auth on the RAW bytes ───────────────────────────────
    try:
        parsed: Any = json.loads(raw_body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = None

    body_token = parsed.get("token") if isinstance(parsed, dict) else None
    if not authenticate_webhook(
        raw_body=raw_body,
        header=x_atlas_signature,
        body_token=body_token,
        hmac_secret=settings.webhook_hmac_secret,
        expected_token=settings.webhook_token,
    ):
        logger.warning("webhook rejected: bad %s", "signature" if x_atlas_signature else "token")
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "webhook authentication failed"},
        )
    auth_method = "hmac" if x_atlas_signature else "token"

    # ── 3. kill switch ─────────────────────────────────────────────────────
    kill_state = await _kill_switch_state()
    if kill_state.get("engaged"):
        return JSONResponse(
            status_code=status.HTTP_423_LOCKED,
            content={
                "detail": "kill switch engaged; no new signals accepted",
                "reason": kill_state.get("reason"),
            },
        )

    # ── 4. parse the frozen payload ────────────────────────────────────────
    if not isinstance(parsed, dict):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": "request body is not a JSON object"},
        )
    try:
        signal = TradingViewSignal.model_validate(parsed)
    except ValidationError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": json.loads(exc.json())},
        )

    signal_id = uuid4().hex
    now = datetime.now(timezone.utc)
    ttl = int(settings.confirmation_ttl_seconds)
    expires_at = now + timedelta(seconds=ttl)

    # ── 5. risk engine ─────────────────────────────────────────────────────
    daily_pnl, open_positions = await _risk_context()
    decision = evaluate_risk(
        symbol=signal.symbol,
        venue=signal.venue.value,
        entry_price=signal.limit_price,
        stop_loss=signal.stop_loss,
        risk_pct=signal.risk_pct,
        daily_pnl=daily_pnl,
        open_positions=open_positions,
        settings=settings,
    )

    setup = PendingSetup(
        signal_id=signal_id,
        status=SignalStatus.PENDING_APPROVAL.value,
        symbol=signal.symbol,
        venue=signal.venue,
        action=signal.action,
        regime=signal.regime,
        limit_price=signal.limit_price,
        stop_loss=signal.stop_loss,
        take_profit_1=signal.take_profit_1,
        take_profit_2=signal.take_profit_2,
        risk_pct=signal.risk_pct,
        qty=decision.qty,
        risk_amount=decision.risk_amount,
        stop_distance=decision.stop_distance,
        risk_approved=decision.approved,
        risk_reason=decision.reason,
        created_at=now,
        expires_at=expires_at,
        ttl_seconds=ttl,
    )

    # ── 6. audit ledger (best effort) ──────────────────────────────────────
    await ledger.record_signal(
        signal_id=signal_id,
        symbol=signal.symbol,
        venue=signal.venue.value,
        action=signal.action.value,
        regime=signal.regime.value,
        limit_price=signal.limit_price,
        stop_loss=signal.stop_loss,
        take_profit_1=signal.take_profit_1,
        take_profit_2=signal.take_profit_2,
        risk_pct=signal.risk_pct,
        status=SignalStatus.PENDING_APPROVAL.value,
        qty=decision.qty,
        risk_amount=decision.risk_amount,
        risk_approved=decision.approved,
        risk_reason=decision.reason,
        source_ip=request.client.host if request.client else None,
        auth_method=auth_method,
        raw_payload={k: v for k, v in parsed.items() if k != "token"},
        expires_at=expires_at,
    )

    # ── 7. Redis SETEX hold ────────────────────────────────────────────────
    setup_json: Dict[str, Any] = setup.model_dump(mode="json")
    try:
        await redis_client.store_pending_setup(signal_id, setup_json, ttl)
    except Exception:
        logger.error("redis unavailable; cannot hold setup %s", signal_id, exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "confirmation store unavailable"},
        )

    # ── 8. FCM actionable push — NEVER fatal ───────────────────────────────
    try:
        push = await fcm.send_actionable_setup(setup_json, settings=settings)
        logger.info("FCM push %s (mock=%s)", push.get("message_id"), push.get("mock"))
    except Exception:  # pragma: no cover - fcm already swallows everything
        logger.warning("FCM push raised despite its own guard", exc_info=True)

    # ── 9. broadcast + ack ─────────────────────────────────────────────────
    await _broadcast_pending(setup_json)

    response.status_code = status.HTTP_200_OK
    return WebhookAck(
        signal_id=signal_id,
        status=SignalStatus.PENDING_APPROVAL.value,
        expires_in=ttl,
    )


# ── helpers ────────────────────────────────────────────────────────────────
async def _kill_switch_state() -> Dict[str, Any]:
    try:
        return await redis_client.kill_switch_state()
    except Exception:
        logger.warning("kill-switch read failed; assuming released", exc_info=True)
        return {"engaged": False, "degraded": True}


async def _risk_context() -> tuple[float, int]:
    """Daily P&L + open-position count, defaulting to a permissive 0/0."""
    day = datetime.now(timezone.utc).date().isoformat()
    try:
        return await redis_client.get_daily_pnl(day), await redis_client.get_open_position_count()
    except Exception:
        return 0.0, 0


async def _broadcast_pending(setup_json: Dict[str, Any]) -> None:
    try:
        from app.main import dashboard_hub

        await dashboard_hub.broadcast({"type": "setup_pending", "setup": setup_json})
    except Exception:  # pragma: no cover - hub is optional
        logger.debug("dashboard broadcast skipped", exc_info=True)
