"""Audit-ledger writes.

Every function here is best-effort: when PostgreSQL is absent (tests, degraded
production) the write is skipped and logged rather than failing the trade.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.core import db

logger = logging.getLogger(__name__)


async def record_signal(
    *,
    signal_id: str,
    symbol: str,
    venue: str,
    action: str,
    regime: str,
    limit_price: float,
    stop_loss: float,
    take_profit_1: float,
    take_profit_2: float,
    risk_pct: float,
    status: str,
    qty: float = 0.0,
    risk_amount: float = 0.0,
    risk_approved: bool = True,
    risk_reason: Optional[str] = None,
    source_ip: Optional[str] = None,
    auth_method: Optional[str] = None,
    raw_payload: Optional[Dict[str, Any]] = None,
    expires_at: Optional[datetime] = None,
) -> bool:
    """Insert one row into ``signals``."""
    if not db.is_enabled():
        return False
    try:
        from app.models.tables import Signal

        async with db.session_scope() as session:
            if session is None:
                return False
            session.add(
                Signal(
                    signal_id=signal_id,
                    symbol=symbol,
                    venue=venue,
                    action=action,
                    regime=regime,
                    limit_price=limit_price,
                    stop_loss=stop_loss,
                    take_profit_1=take_profit_1,
                    take_profit_2=take_profit_2,
                    risk_pct=risk_pct,
                    status=status,
                    qty=qty,
                    risk_amount=risk_amount,
                    risk_approved=risk_approved,
                    risk_reason=risk_reason,
                    source_ip=source_ip,
                    auth_method=auth_method,
                    raw_payload=raw_payload,
                    expires_at=expires_at,
                )
            )
        return True
    except Exception:
        logger.warning("ledger: record_signal failed for %s", signal_id, exc_info=True)
        return False


async def update_signal_status(
    signal_id: str, status: str, reason: Optional[str] = None, qty: Optional[float] = None
) -> bool:
    """Patch ``signals.status`` after a state-machine transition."""
    if not db.is_enabled():
        return False
    try:
        from sqlalchemy import update

        from app.models.tables import Signal

        values: Dict[str, Any] = {
            "status": status,
            "updated_at": datetime.now(timezone.utc),
        }
        if reason is not None:
            values["risk_reason"] = reason
        if qty is not None:
            values["qty"] = qty

        async with db.session_scope() as session:
            if session is None:
                return False
            await session.execute(
                update(Signal).where(Signal.signal_id == signal_id).values(**values)
            )
        return True
    except Exception:
        logger.warning("ledger: update_signal_status failed for %s", signal_id, exc_info=True)
        return False


async def record_confirmation(
    *,
    signal_id: str,
    decision: str,
    previous_status: str,
    new_status: str,
    venue: Optional[str] = None,
    actor: str = "mobile",
    reason: Optional[str] = None,
    latency_ms: Optional[int] = None,
) -> bool:
    """Insert one row into ``confirmations``."""
    if not db.is_enabled():
        return False
    try:
        from app.models.tables import Confirmation

        async with db.session_scope() as session:
            if session is None:
                return False
            session.add(
                Confirmation(
                    signal_id=signal_id,
                    decision=decision,
                    venue=venue,
                    actor=actor,
                    previous_status=previous_status,
                    new_status=new_status,
                    reason=reason,
                    latency_ms=latency_ms,
                )
            )
        return True
    except Exception:
        logger.warning("ledger: record_confirmation failed for %s", signal_id, exc_info=True)
        return False


async def record_order(
    *,
    signal_id: str,
    venue: str,
    symbol: str,
    action: str,
    qty: float,
    limit_price: Optional[float] = None,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    broker_order_id: Optional[str] = None,
    leverage: Optional[int] = None,
    status: str = "ROUTED",
    is_mock: bool = True,
    request_payload: Optional[Dict[str, Any]] = None,
    response_payload: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> bool:
    """Insert one row into ``orders``."""
    if not db.is_enabled():
        return False
    try:
        from app.models.tables import Order

        async with db.session_scope() as session:
            if session is None:
                return False
            session.add(
                Order(
                    signal_id=signal_id,
                    venue=venue,
                    broker_order_id=broker_order_id,
                    symbol=symbol,
                    action=action,
                    qty=qty,
                    limit_price=limit_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    leverage=leverage,
                    status=status,
                    is_mock=is_mock,
                    request_payload=request_payload,
                    response_payload=response_payload,
                    error=error,
                )
            )
        return True
    except Exception:
        logger.warning("ledger: record_order failed for %s", signal_id, exc_info=True)
        return False


async def record_agent_run(
    *,
    run_id: str,
    agent: str,
    status: str = "COMPLETED",
    regime: Optional[str] = None,
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
    candidates_created: int = 0,
    candidates_passed: int = 0,
    best_dsr: Optional[float] = None,
    duration_ms: Optional[int] = None,
    payload: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> bool:
    """Insert one row into ``agent_runs``."""
    if not db.is_enabled():
        return False
    try:
        from app.models.tables import AgentRun

        async with db.session_scope() as session:
            if session is None:
                return False
            session.add(
                AgentRun(
                    run_id=run_id,
                    agent=agent,
                    status=status,
                    regime=regime,
                    symbol=symbol,
                    timeframe=timeframe,
                    candidates_created=candidates_created,
                    candidates_passed=candidates_passed,
                    best_dsr=best_dsr,
                    duration_ms=duration_ms,
                    payload=payload,
                    error=error,
                    finished_at=datetime.now(timezone.utc),
                )
            )
        return True
    except Exception:
        logger.warning("ledger: record_agent_run failed for %s", run_id, exc_info=True)
        return False
