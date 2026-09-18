"""SQLAlchemy 2.0 ORM — a 1:1 mirror of ``backend/sql/init/001_schema.sql``.

JSON columns use the dialect-agnostic :class:`sqlalchemy.JSON` so the same
metadata can also be created against SQLite in tests.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for the ATLAS audit ledger."""


PRICE = Numeric(20, 8)


class Signal(Base):
    """Every validated TradingView alert (artifact §3 payload + verdict)."""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    venue: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(8), nullable=False)
    regime: Mapped[str] = mapped_column(String(32), nullable=False)
    limit_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    stop_loss: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    take_profit_1: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    take_profit_2: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    risk_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="PENDING_APPROVAL"
    )
    qty: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=0)
    risk_amount: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=0)
    risk_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    risk_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    strategy_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    source_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    auth_method: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    raw_payload: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_signals_created_at", created_at.desc()),
        Index("ix_signals_status", status),
        Index("ix_signals_symbol", symbol),
    )


class Confirmation(Base):
    """The HITL tap — or the TTL expiry that fired instead of one."""

    __tablename__ = "confirmations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("signals.signal_id", ondelete="CASCADE"), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    venue: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    actor: Mapped[str] = mapped_column(String(64), nullable=False, default="mobile")
    previous_status: Mapped[str] = mapped_column(String(24), nullable=False)
    new_status: Mapped[str] = mapped_column(String(24), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_confirmations_signal_id", signal_id),)


class Order(Base):
    """One row per broker submission (mock submissions included)."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("signals.signal_id", ondelete="CASCADE"), nullable=False
    )
    venue: Mapped[str] = mapped_column(String(16), nullable=False)
    broker_order_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False, default="Limit")
    qty: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    limit_price: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)
    stop_loss: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)
    take_profit: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)
    leverage: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="ROUTED")
    is_mock: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    request_payload: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    response_payload: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_orders_signal_id", signal_id),
        Index("ix_orders_venue", venue),
    )


class Fill(Base):
    """Execution reports against an :class:`Order`."""

    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("orders.id", ondelete="CASCADE"), nullable=True
    )
    signal_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    venue: Mapped[str] = mapped_column(String(16), nullable=False)
    broker_fill_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(8), nullable=False)
    qty: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    fee: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=0)
    realized_pnl: Mapped[Optional[Decimal]] = mapped_column(PRICE, nullable=True)
    is_mock: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    filled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_fills_order_id", order_id),
        Index("ix_fills_signal_id", signal_id),
    )


class AgentRun(Base):
    """LAYER 5 research audit trail (ideation / backtest / allocator passes)."""

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    agent: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="COMPLETED")
    regime: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    symbol: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    timeframe: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    candidates_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidates_passed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    best_dsr: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6), nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    payload: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_agent_runs_agent", agent),
        Index("ix_agent_runs_started_at", started_at.desc()),
    )


class DailyPnl(Base):
    """Per-day realised P&L — the source of the drawdown lockout."""

    __tablename__ = "daily_pnl"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    venue: Mapped[str] = mapped_column(String(16), nullable=False, default="ALL")
    starting_equity: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=0)
    realized_pnl: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=0)
    unrealized_pnl: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=0)
    drawdown_pct: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, default=0
    )
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    win_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lockout_engaged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("trade_date", "venue", name="uq_daily_pnl_date_venue"),
        Index("ix_daily_pnl_trade_date", trade_date.desc()),
    )


__all__ = [
    "Base",
    "Signal",
    "Confirmation",
    "Order",
    "Fill",
    "AgentRun",
    "DailyPnl",
]
