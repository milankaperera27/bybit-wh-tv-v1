"""HITL 1-tap confirmation routes — FROZEN contract B (artifact §4).

| Method | Path |
|--------|------|
| GET    | ``/confirmation/pending`` |
| GET    | ``/confirmation/{signal_id}`` |
| POST   | ``/confirmation/{signal_id}/confirm`` |
| POST   | ``/confirmation/{signal_id}/reject`` |
| POST   | ``/confirmation/{signal_id}/expire`` (janitor / test hook) |

The single most important invariant: **an expired setup causes ZERO broker
calls.**  Expiry is detected purely by the absence of the Redis key, which is
checked before any client is constructed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Path, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.brokers import get_broker
from app.brokers.base import BrokerError
from app.core import ledger, redis_client
from app.core.config import get_settings
from app.core.risk_engine import evaluate as evaluate_risk
from app.core.state_machine import IllegalTransition, SignalStatus, transition
from app.models.schemas import ConfirmRequest, OrderResult, PendingSetup, RejectRequest, Venue

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/confirmation", tags=["confirmation"])

_GONE_DETAIL = (
    "setup expired: the confirmation window elapsed; no broker interaction occurred"
)


# ── reads ──────────────────────────────────────────────────────────────────
@router.get("/pending", response_model=List[PendingSetup])
async def list_pending() -> Any:
    """Every live (unexpired) setup held in Redis.

    A record that no longer matches the schema — left over from an older build,
    or hand-written into Redis — is skipped rather than raised.  Validating the
    whole list eagerly meant one stale key returned HTTP 500 and blanked the
    dashboard, hiding every *healthy* setup alongside the bad one.
    """
    try:
        raw = await redis_client.list_pending_setups()
    except Exception:
        logger.warning("pending list unavailable", exc_info=True)
        return []

    setups: List[PendingSetup] = []
    for item in raw:
        try:
            setups.append(PendingSetup.model_validate(item))
        except ValidationError:
            signal_id = item.get("signal_id") if isinstance(item, dict) else None
            logger.error(
                "dropping unparseable pending setup %s; delete its Redis key to silence this",
                signal_id or "<unknown>",
                exc_info=True,
            )
    return setups


@router.get(
    "/{signal_id}",
    response_model=PendingSetup,
    responses={410: {"description": "Confirmation window elapsed"}},
)
async def get_setup(signal_id: str = Path(..., min_length=1)) -> Any:
    setup = await _load(signal_id)
    if setup is None:
        return JSONResponse(status_code=status.HTTP_410_GONE, content={"detail": _GONE_DETAIL})
    try:
        return PendingSetup.model_validate(setup)
    except ValidationError:
        logger.error("pending setup %s is unparseable; treating as gone", signal_id, exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_410_GONE,
            content={"detail": "setup record is unreadable and cannot be acted on"},
        )


# ── the tap ────────────────────────────────────────────────────────────────
@router.post(
    "/{signal_id}/confirm",
    response_model=OrderResult,
    responses={
        410: {"description": "Confirmation window elapsed — zero broker calls"},
        422: {"description": "Risk engine vetoed the order"},
        423: {"description": "Global kill switch engaged"},
        502: {"description": "Venue rejected the order"},
    },
)
async def confirm_setup(
    signal_id: str = Path(..., min_length=1),
    body: ConfirmRequest = Body(...),
) -> Any:
    """Route a held setup to the chosen venue after a fresh risk re-check."""
    settings = get_settings()

    # 1. TTL check FIRST — an expired setup must never reach a broker client.
    setup = await _load(signal_id)
    if setup is None:
        await ledger.update_signal_status(signal_id, SignalStatus.EXPIRED.value, "ttl_elapsed")
        await ledger.record_confirmation(
            signal_id=signal_id,
            decision="EXPIRED",
            previous_status=SignalStatus.PENDING_APPROVAL.value,
            new_status=SignalStatus.EXPIRED.value,
            actor=body.actor,
            reason="ttl_elapsed",
        )
        return JSONResponse(status_code=status.HTTP_410_GONE, content={"detail": _GONE_DETAIL})

    current = SignalStatus(str(setup.get("status", SignalStatus.PENDING_APPROVAL.value)))

    # 2. kill switch re-check.
    kill_state = await _kill_state()
    if kill_state.get("engaged"):
        await _terminate(signal_id, setup, current, SignalStatus.KILLED, "kill_switch", body.actor)
        return JSONResponse(
            status_code=status.HTTP_423_LOCKED,
            content={"detail": "kill switch engaged", "reason": kill_state.get("reason")},
        )

    # 3. PENDING_APPROVAL -> CONFIRMED.
    try:
        confirmed = transition(current, SignalStatus.CONFIRMED, reason="user_tap")
    except IllegalTransition as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)}
        )

    # 4. risk re-check against live counters.
    venue = body.venue
    daily_pnl, open_positions = await _risk_context()
    decision = evaluate_risk(
        symbol=setup["symbol"],
        venue=venue.value,
        entry_price=float(setup["limit_price"]),
        stop_loss=float(setup["stop_loss"]),
        risk_pct=float(setup["risk_pct"]),
        daily_pnl=daily_pnl,
        open_positions=open_positions,
        settings=settings,
    )
    qty = float(body.qty_override) if body.qty_override else decision.qty
    if not decision.approved or qty <= 0:
        await _terminate(
            signal_id,
            setup,
            confirmed.current,
            SignalStatus.REJECTED_RISK,
            decision.reason,
            body.actor,
            venue=venue.value,
        )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "detail": "risk engine rejected the order",
                "reason": decision.reason,
                "signal_id": signal_id,
                "status": SignalStatus.REJECTED_RISK.value,
            },
        )

    # 5. route to the venue.
    client = get_broker(venue.value)
    try:
        broker_response = await client.place_bracket_order(
            symbol=setup["symbol"],
            action=str(setup["action"]),
            qty=qty,
            limit_price=float(setup["limit_price"]),
            stop_loss=float(setup["stop_loss"]),
            take_profit=float(setup["take_profit_1"]),
        )
    except BrokerError as exc:
        await _terminate(
            signal_id, setup, confirmed.current, SignalStatus.REJECTED, str(exc), body.actor,
            venue=venue.value,
        )
        await redis_client_delete(signal_id)
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": str(exc), "signal_id": signal_id},
        )

    routed = transition(confirmed.current, SignalStatus.ROUTED, reason="broker_accepted")
    broker_order_id = _extract_order_id(broker_response)
    is_mock = bool(broker_response.get("mock", False))

    # 6. persist + advance the ledger.
    await ledger.record_order(
        signal_id=signal_id,
        venue=venue.value,
        symbol=str(setup["symbol"]),
        action=str(setup["action"]),
        qty=qty,
        limit_price=float(setup["limit_price"]),
        stop_loss=float(setup["stop_loss"]),
        take_profit=float(setup["take_profit_1"]),
        broker_order_id=broker_order_id,
        leverage=settings.kraken_default_leverage if venue is Venue.KRAKEN else None,
        status=routed.current.value,
        is_mock=is_mock,
        request_payload=broker_response.get("payload"),
        response_payload={k: v for k, v in broker_response.items() if k != "payload"},
    )
    await ledger.record_confirmation(
        signal_id=signal_id,
        decision="CONFIRM",
        venue=venue.value,
        actor=body.actor,
        previous_status=current.value,
        new_status=routed.current.value,
        reason="user_tap",
        latency_ms=_latency_ms(setup),
    )
    await ledger.update_signal_status(signal_id, routed.current.value, "routed", qty=qty)

    # 7. release the Redis hold — the setup is no longer pending.
    await redis_client_delete(signal_id)
    await _broadcast({"type": "setup_resolved", "signal_id": signal_id, "status": SignalStatus.ROUTED.value, "venue": venue.value})

    return OrderResult(
        signal_id=signal_id,
        venue=venue,
        status=routed.current.value,
        broker_order_id=broker_order_id,
        symbol=str(setup["symbol"]),
        action=str(setup["action"]),
        qty=qty,
        limit_price=float(setup["limit_price"]),
        stop_loss=float(setup["stop_loss"]),
        take_profit=float(setup["take_profit_1"]),
        mock=is_mock,
        raw=broker_response,
    )


@router.post(
    "/{signal_id}/reject",
    responses={410: {"description": "Confirmation window already elapsed"}},
)
async def reject_setup(
    signal_id: str = Path(..., min_length=1),
    body: RejectRequest = Body(default=RejectRequest()),
) -> Any:
    """Trader tapped Reject — terminal, zero broker interaction."""
    setup = await _load(signal_id)
    if setup is None:
        return JSONResponse(status_code=status.HTTP_410_GONE, content={"detail": _GONE_DETAIL})
    current = SignalStatus(str(setup.get("status", SignalStatus.PENDING_APPROVAL.value)))
    try:
        result = transition(current, SignalStatus.REJECTED, reason=body.reason)
    except IllegalTransition as exc:
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})

    await ledger.record_confirmation(
        signal_id=signal_id,
        decision="REJECT",
        actor=body.actor,
        previous_status=current.value,
        new_status=result.current.value,
        reason=body.reason,
        latency_ms=_latency_ms(setup),
    )
    await ledger.update_signal_status(signal_id, result.current.value, body.reason)
    await redis_client_delete(signal_id)
    await _broadcast({"type": "setup_resolved", "signal_id": signal_id, "status": SignalStatus.REJECTED.value})
    return {"signal_id": signal_id, "status": result.current.value, "reason": body.reason}


@router.post("/{signal_id}/expire")
async def expire_setup(signal_id: str = Path(..., min_length=1)) -> Any:
    """Explicitly mark a setup EXPIRED (janitor sweep / test hook)."""
    setup = await _load(signal_id)
    previous = (
        SignalStatus(str(setup.get("status", SignalStatus.PENDING_APPROVAL.value)))
        if setup
        else SignalStatus.PENDING_APPROVAL
    )
    try:
        result = transition(previous, SignalStatus.EXPIRED, reason="ttl_elapsed")
    except IllegalTransition as exc:
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})
    await redis_client_delete(signal_id)
    await ledger.update_signal_status(signal_id, result.current.value, "ttl_elapsed")
    await ledger.record_confirmation(
        signal_id=signal_id,
        decision="EXPIRED",
        previous_status=previous.value,
        new_status=result.current.value,
        actor="system",
        reason="ttl_elapsed",
    )
    return {"signal_id": signal_id, "status": result.current.value, "broker_calls": 0}


# ── helpers ────────────────────────────────────────────────────────────────
async def _load(signal_id: str) -> Optional[Dict[str, Any]]:
    try:
        return await redis_client.load_pending_setup(signal_id)
    except Exception:
        logger.warning("redis read failed for %s", signal_id, exc_info=True)
        return None


async def redis_client_delete(signal_id: str) -> None:
    try:
        await redis_client.delete_pending_setup(signal_id)
    except Exception:
        logger.warning("redis delete failed for %s", signal_id, exc_info=True)


async def _kill_state() -> Dict[str, Any]:
    try:
        return await redis_client.kill_switch_state()
    except Exception:
        return {"engaged": False, "degraded": True}


async def _risk_context() -> tuple[float, int]:
    day = datetime.now(timezone.utc).date().isoformat()
    try:
        return await redis_client.get_daily_pnl(day), await redis_client.get_open_position_count()
    except Exception:
        return 0.0, 0


async def _terminate(
    signal_id: str,
    setup: Dict[str, Any],
    current: SignalStatus,
    target: SignalStatus,
    reason: str,
    actor: str,
    venue: Optional[str] = None,
) -> None:
    """Drive a setup to a terminal state and release its Redis hold."""
    try:
        result = transition(current, target, reason=reason)
        new_status = result.current.value
    except IllegalTransition:
        new_status = target.value
    await ledger.record_confirmation(
        signal_id=signal_id,
        decision=target.value,
        venue=venue,
        actor=actor,
        previous_status=current.value,
        new_status=new_status,
        reason=reason,
        latency_ms=_latency_ms(setup),
    )
    await ledger.update_signal_status(signal_id, new_status, reason)
    await redis_client_delete(signal_id)


def _latency_ms(setup: Dict[str, Any]) -> Optional[int]:
    created = setup.get("created_at")
    if not created:
        return None
    try:
        started = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - started).total_seconds() * 1000))
    except ValueError:
        return None


def _extract_order_id(response: Dict[str, Any]) -> Optional[str]:
    """Normalise Tradovate ``orderId`` / Kraken ``result.txid[0]``."""
    if "orderId" in response and response["orderId"] is not None:
        return str(response["orderId"])
    result = response.get("result")
    if isinstance(result, dict):
        txid = result.get("txid")
        if isinstance(txid, list) and txid:
            return str(txid[0])
        if isinstance(txid, str):
            return txid
    return None


async def _broadcast(frame: Dict[str, Any]) -> None:
    try:
        from app.main import dashboard_hub

        await dashboard_hub.broadcast(frame)
    except Exception:  # pragma: no cover
        logger.debug("dashboard broadcast skipped", exc_info=True)
