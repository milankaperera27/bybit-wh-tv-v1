"""Pydantic v2 wire models.

:class:`TradingViewSignal` is the FROZEN contract A of the implementation
artifact (§3) — emitted verbatim by ``Atlas_Execution_Suite.pine``.  Do not add
required fields to it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ── Enums (frozen vocabulary, artifact §3) ─────────────────────────────────


class Venue(str, Enum):
    TRADOVATE = "TRADOVATE"
    KRAKEN = "KRAKEN"


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Regime(str, Enum):
    BULL_TREND_EXPANSION = "BULL_TREND_EXPANSION"
    BEAR_TREND_EXPANSION = "BEAR_TREND_EXPANSION"
    RANGE_COMPRESSION = "RANGE_COMPRESSION"
    VOLATILITY_CHOP = "VOLATILITY_CHOP"


# ── FROZEN CONTRACT A ──────────────────────────────────────────────────────


class TradingViewSignal(BaseModel):
    """Exactly the artifact §3 payload — nothing more, nothing less.

    ```json
    {
      "token": "SECRET_TOKEN", "symbol": "NQ1!", "venue": "TRADOVATE",
      "action": "BUY", "limit_price": 20155.25, "stop_loss": 20140.00,
      "take_profit_1": 20178.12, "take_profit_2": 20201.00,
      "regime": "BULL_TREND_EXPANSION", "risk_pct": 1.0
    }
    ```
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    token: str = Field(..., description="Shared secret embedded in the Pine input")
    symbol: str = Field(..., min_length=1, max_length=32)
    venue: Venue
    action: Action
    limit_price: float = Field(..., gt=0)
    stop_loss: float = Field(..., gt=0)
    take_profit_1: float = Field(..., gt=0)
    take_profit_2: float = Field(..., gt=0)
    regime: Regime
    risk_pct: float = Field(..., gt=0, le=100)

    @field_validator("venue", "action", "regime", mode="before")
    @classmethod
    def _upper(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @property
    def is_long(self) -> bool:
        return self.action is Action.BUY

    @property
    def primary_take_profit(self) -> float:
        """TP1 is the bracket target routed to the venue (TP2 is a scale-out)."""
        return self.take_profit_1


# ── HITL surface ───────────────────────────────────────────────────────────


class PendingSetup(BaseModel):
    """What lives in Redis under a ``settings.confirmation_ttl_seconds`` TTL."""

    model_config = ConfigDict(extra="ignore")

    signal_id: str = Field(default_factory=lambda: uuid4().hex)
    status: str = "PENDING_APPROVAL"
    symbol: str
    venue: Venue
    action: Action
    regime: Regime
    limit_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    risk_pct: float
    qty: float = 0.0
    risk_amount: float = 0.0
    stop_distance: float = 0.0
    risk_approved: bool = True
    risk_reason: str = "OK"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = None
    ttl_seconds: int = 60

    def seconds_remaining(self, now: Optional[datetime] = None) -> int:
        if self.expires_at is None:
            return 0
        current = now or datetime.now(timezone.utc)
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return max(0, int((expires - current).total_seconds()))


class ConfirmRequest(BaseModel):
    """Body of ``POST /api/v1/confirmation/{signal_id}/confirm``."""

    model_config = ConfigDict(extra="ignore")

    venue: Venue
    qty_override: Optional[float] = Field(default=None, gt=0)
    actor: str = "mobile"


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reason: str = "user_rejected"
    actor: str = "mobile"


class RiskDecision(BaseModel):
    """Wire form of :class:`app.core.risk_engine.RiskDecision`."""

    model_config = ConfigDict(extra="ignore")

    approved: bool
    reason: str
    qty: float = 0.0
    risk_amount: float = 0.0
    stop_distance: float = 0.0
    risk_per_unit: float = 0.0
    uncapped_qty: float = 0.0
    clamped: bool = False
    details: Dict[str, Any] = Field(default_factory=dict)


class OrderResult(BaseModel):
    """Normalised broker response returned to the mobile client."""

    model_config = ConfigDict(extra="ignore")

    signal_id: str
    venue: Venue
    status: str
    broker_order_id: Optional[str] = None
    symbol: Optional[str] = None
    action: Optional[Action] = None
    qty: float = 0.0
    limit_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    mock: bool = False
    raw: Dict[str, Any] = Field(default_factory=dict)
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class KillSwitchStatus(BaseModel):
    """``GET /api/v1/emergency/status`` payload."""

    model_config = ConfigDict(extra="ignore")

    engaged: bool = False
    reason: Optional[str] = None
    actor: Optional[str] = None
    engaged_at: Optional[datetime] = None
    released_at: Optional[datetime] = None
    daily_pnl: float = 0.0
    daily_drawdown_pct: float = 0.0
    drawdown_lockout: bool = False
    open_positions: int = 0
    pending_setups: int = 0
    degraded: bool = False


class WebhookAck(BaseModel):
    """``POST /api/v1/webhooks/tradingview`` 200 body."""

    signal_id: str
    status: str = "PENDING_APPROVAL"
    expires_in: int = 60


class HealthStatus(BaseModel):
    status: str = "ok"
    version: str = "1.0.0"
    env: str = "development"
    mock_brokers: bool = True
    redis: str = "down"
    postgres: str = "down"
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class FlattenResult(BaseModel):
    tradovate: Dict[str, Any] = Field(default_factory=dict)
    kraken: Dict[str, Any] = Field(default_factory=dict)
    pending_purged: int = 0
    kill_switch_engaged: bool = True


class DashboardFrame(BaseModel):
    """Payload broadcast over ``WS /ws/dashboard``."""

    type: str = "snapshot"
    regime: Optional[str] = None
    pending: List[PendingSetup] = Field(default_factory=list)
    kill_switch: bool = False
    emitted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
