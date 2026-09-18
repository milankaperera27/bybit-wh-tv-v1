"""Candidate Strategy DSL (spec §2.1).

Agent 2 (Ideation) emits these; Agent 3 (Validation) fills in
``validation_metrics`` and Agent 4 (Allocator) promotes the survivors onto the
active roster.  The JSON shape mirrors the spec example byte-for-byte.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.schemas import Regime, Venue


class TakeProfitLevel(BaseModel):
    """One rung of the scale-out ladder: ``{"ratio": 1.5, "size_pct": 50}``."""

    model_config = ConfigDict(extra="forbid")

    ratio: float = Field(..., gt=0, description="Reward multiple of the initial risk (R)")
    size_pct: float = Field(..., gt=0, le=100, description="Percent of the position closed")


class Primitives(BaseModel):
    """SMC primitive tree produced by the genetic synthesiser."""

    model_config = ConfigDict(extra="allow")

    structure_trigger: str = Field(default="MSS_CONFIRMED")
    liquidity_sweep: Optional[str] = Field(default=None)
    entry_zone: Optional[str] = Field(default=None)
    filter_trend_ema: int = Field(default=200, ge=1, le=1000)
    volume_filter: Optional[str] = Field(default=None)


class RiskParameters(BaseModel):
    """Per-strategy risk envelope."""

    model_config = ConfigDict(extra="allow")

    risk_per_trade_pct: float = Field(default=1.0, gt=0, le=5.0)
    max_contracts: int = Field(default=4, ge=1)
    stop_loss_type: str = Field(default="SWING_LOW_OFFSET")
    stop_offset_ticks: int = Field(default=4, ge=0)
    take_profit_levels: List[TakeProfitLevel] = Field(
        default_factory=lambda: [
            TakeProfitLevel(ratio=1.5, size_pct=50),
            TakeProfitLevel(ratio=3.0, size_pct=50),
        ]
    )
    breakeven_trigger_r: float = Field(default=1.0, ge=0)

    @model_validator(mode="after")
    def _ladder_must_not_exceed_position(self) -> "RiskParameters":
        total = sum(level.size_pct for level in self.take_profit_levels)
        if total > 100.0 + 1e-9:
            raise ValueError(
                f"take_profit_levels size_pct sums to {total}; must be <= 100"
            )
        return self


class ValidationMetrics(BaseModel):
    """Agent 3 scorecard.  ``deflated_sharpe_ratio`` is the production gate."""

    model_config = ConfigDict(extra="allow")

    in_sample_sharpe: float = 0.0
    out_sample_sharpe: float = 0.0
    deflated_sharpe_ratio: float = 0.0
    max_drawdown_pct: float = 0.0
    profit_factor: float = 0.0
    trade_count: int = 0
    monte_carlo_draws: int = 0
    mc_p05_return_pct: Optional[float] = None
    mc_p95_return_pct: Optional[float] = None
    sortino_ratio: Optional[float] = None

    def passes(self, dsr_threshold: float = 1.5) -> bool:
        return self.deflated_sharpe_ratio >= dsr_threshold


class StrategyDSL(BaseModel):
    """The full §2.1 document."""

    model_config = ConfigDict(extra="allow")

    strategy_id: str = Field(..., min_length=1)
    symbol: str = Field(..., min_length=1)
    venue: Venue = Venue.TRADOVATE
    compatible_regimes: List[Regime] = Field(default_factory=list)
    timeframe: str = Field(default="5m")
    primitives: Primitives = Field(default_factory=Primitives)
    risk_parameters: RiskParameters = Field(default_factory=RiskParameters)
    validation_metrics: ValidationMetrics = Field(default_factory=ValidationMetrics)

    # ── Allocator bookkeeping (not part of the spec example) ───────────────
    generation: int = 0
    parent_ids: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    active: bool = False

    @field_validator("venue", mode="before")
    @classmethod
    def _upper_venue(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @field_validator("compatible_regimes", mode="before")
    @classmethod
    def _upper_regimes(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [v.upper() if isinstance(v, str) else v for v in value]
        return value

    def is_compatible_with(self, regime: "Regime | str") -> bool:
        target = regime if isinstance(regime, Regime) else Regime(str(regime).upper())
        return not self.compatible_regimes or target in self.compatible_regimes

    def passes_validation(self, dsr_threshold: float = 1.5) -> bool:
        return self.validation_metrics.passes(dsr_threshold)

    def to_spec_json(self) -> Dict[str, Any]:
        """Serialise back to the exact §2.1 key set (allocator fields dropped)."""
        return {
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "venue": self.venue.value,
            "compatible_regimes": [r.value for r in self.compatible_regimes],
            "timeframe": self.timeframe,
            "primitives": self.primitives.model_dump(exclude_none=True),
            "risk_parameters": self.risk_parameters.model_dump(),
            "validation_metrics": self.validation_metrics.model_dump(exclude_none=True),
        }
