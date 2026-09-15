"""Agent 4 — Strategy Allocator (spec §2, LAYER 5).

Takes the DSR survivors from Agent 3 and maintains the **active production
roster**:

1. **Rolling Sortino ranking** over each strategy's recent return window.
2. **Correlation filter** — a candidate whose returns correlate above the
   threshold with an already-admitted, higher-ranked strategy is dropped.
3. **Roster management** — capacity cap, regime compatibility, and equal-risk
   (inverse-volatility optional) weight allocation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence
from uuid import uuid4

import numpy as np

from app.agents.backtest_agent import sortino_ratio
from app.models.schemas import Regime
from app.models.strategy_dsl import StrategyDSL

logger = logging.getLogger(__name__)

DEFAULT_MAX_ROSTER = 5
DEFAULT_CORRELATION_THRESHOLD = 0.7
DEFAULT_SORTINO_WINDOW = 120


@dataclass
class RosterEntry:
    """One admitted strategy plus its allocation."""

    strategy: StrategyDSL
    sortino: float
    dsr: float
    weight: float = 0.0
    rank: int = 0
    max_correlation: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_id": self.strategy.strategy_id,
            "symbol": self.strategy.symbol,
            "venue": self.strategy.venue.value,
            "timeframe": self.strategy.timeframe,
            "regimes": [r.value for r in self.strategy.compatible_regimes],
            "sortino": round(self.sortino, 4),
            "dsr": round(self.dsr, 4),
            "weight": round(self.weight, 4),
            "rank": self.rank,
            "max_correlation": round(self.max_correlation, 4),
        }


@dataclass
class AllocationResult:
    """Outcome of one allocation pass."""

    run_id: str
    regime: Optional[Regime]
    roster: List[RosterEntry] = field(default_factory=list)
    rejected: List[Dict[str, Any]] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "regime": self.regime.value if self.regime else None,
            "roster": [e.to_dict() for e in self.roster],
            "rejected": self.rejected,
            "generated_at": self.generated_at.isoformat(),
        }

    @property
    def strategy_ids(self) -> List[str]:
        return [e.strategy.strategy_id for e in self.roster]


def rolling_sortino(
    returns: Sequence[float], window: int = DEFAULT_SORTINO_WINDOW
) -> float:
    """Sortino over the trailing ``window`` observations."""
    r = np.asarray(returns, dtype=float)
    if r.size == 0:
        return 0.0
    return sortino_ratio(r[-window:] if r.size > window else r)


def correlation(a: Sequence[float], b: Sequence[float]) -> float:
    """Pearson correlation over the overlapping tail of two return series."""
    x = np.asarray(a, dtype=float)
    y = np.asarray(b, dtype=float)
    n = min(x.size, y.size)
    if n < 3:
        return 0.0
    x, y = x[-n:], y[-n:]
    if float(np.std(x)) == 0.0 or float(np.std(y)) == 0.0:
        return 0.0
    value = float(np.corrcoef(x, y)[0, 1])
    return 0.0 if np.isnan(value) else value


def correlation_matrix(series: Dict[str, Sequence[float]]) -> Dict[str, Dict[str, float]]:
    """Full pairwise correlation map, keyed by strategy id."""
    keys = list(series)
    return {
        a: {b: (1.0 if a == b else correlation(series[a], series[b])) for b in keys}
        for a in keys
    }


class StrategyOrchestrator:
    """Maintains the active production roster."""

    def __init__(
        self,
        max_roster: int = DEFAULT_MAX_ROSTER,
        correlation_threshold: float = DEFAULT_CORRELATION_THRESHOLD,
        sortino_window: int = DEFAULT_SORTINO_WINDOW,
        dsr_threshold: float = 1.5,
        min_sortino: float = 0.0,
        inverse_volatility_weighting: bool = True,
    ) -> None:
        self.max_roster = max(1, max_roster)
        self.correlation_threshold = correlation_threshold
        self.sortino_window = sortino_window
        self.dsr_threshold = dsr_threshold
        self.min_sortino = min_sortino
        self.inverse_volatility_weighting = inverse_volatility_weighting
        self.roster: List[RosterEntry] = []

    # ── ranking ────────────────────────────────────────────────────────────
    def rank(
        self,
        candidates: Sequence[StrategyDSL],
        returns_by_id: Dict[str, Sequence[float]],
    ) -> List[RosterEntry]:
        """Rolling-Sortino ranking, best first."""
        entries = [
            RosterEntry(
                strategy=c,
                sortino=rolling_sortino(returns_by_id.get(c.strategy_id, []), self.sortino_window),
                dsr=float(c.validation_metrics.deflated_sharpe_ratio),
            )
            for c in candidates
        ]
        entries.sort(key=lambda e: (e.sortino, e.dsr), reverse=True)
        for i, entry in enumerate(entries, start=1):
            entry.rank = i
        return entries

    # ── filters ────────────────────────────────────────────────────────────
    def correlation_filter(
        self,
        ranked: List[RosterEntry],
        returns_by_id: Dict[str, Sequence[float]],
    ) -> tuple[List[RosterEntry], List[Dict[str, Any]]]:
        """Admit in rank order, rejecting anything too close to an incumbent."""
        admitted: List[RosterEntry] = []
        rejected: List[Dict[str, Any]] = []
        for entry in ranked:
            series = returns_by_id.get(entry.strategy.strategy_id, [])
            worst = 0.0
            clash: Optional[str] = None
            for incumbent in admitted:
                rho = abs(
                    correlation(series, returns_by_id.get(incumbent.strategy.strategy_id, []))
                )
                if rho > worst:
                    worst, clash = rho, incumbent.strategy.strategy_id
            entry.max_correlation = worst
            if worst >= self.correlation_threshold:
                rejected.append(
                    {
                        "strategy_id": entry.strategy.strategy_id,
                        "reason": "CORRELATION_FILTER",
                        "correlation": round(worst, 4),
                        "with": clash,
                    }
                )
                continue
            admitted.append(entry)
        return admitted, rejected

    def eligible(
        self, candidate: StrategyDSL, regime: Optional[Regime]
    ) -> Optional[str]:
        """Return a rejection reason, or ``None`` when the candidate qualifies."""
        if candidate.validation_metrics.deflated_sharpe_ratio < self.dsr_threshold:
            return "DSR_BELOW_GATE"
        if regime is not None and not candidate.is_compatible_with(regime):
            return "REGIME_INCOMPATIBLE"
        return None

    # ── allocation ─────────────────────────────────────────────────────────
    def allocate(
        self,
        entries: List[RosterEntry],
        returns_by_id: Dict[str, Sequence[float]],
    ) -> List[RosterEntry]:
        """Assign capital weights: inverse-volatility, else equal-risk."""
        if not entries:
            return entries
        if not self.inverse_volatility_weighting:
            share = 1.0 / len(entries)
            for entry in entries:
                entry.weight = share
            return entries

        inv_vols: List[float] = []
        for entry in entries:
            series = np.asarray(returns_by_id.get(entry.strategy.strategy_id, []), dtype=float)
            vol = float(np.std(series)) if series.size > 1 else 0.0
            inv_vols.append(1.0 / vol if vol > 0 else 1.0)
        total = sum(inv_vols) or 1.0
        for entry, inv in zip(entries, inv_vols):
            entry.weight = inv / total
        return entries

    # ── the pass ───────────────────────────────────────────────────────────
    def build_roster(
        self,
        candidates: Sequence[StrategyDSL],
        returns_by_id: Optional[Dict[str, Sequence[float]]] = None,
        regime: Optional["Regime | str"] = None,
    ) -> AllocationResult:
        """Gate → rank → decorrelate → cap → allocate."""
        series = dict(returns_by_id or {})
        target: Optional[Regime] = None
        if regime is not None:
            target = regime if isinstance(regime, Regime) else Regime(str(regime).upper())

        result = AllocationResult(run_id=uuid4().hex, regime=target)

        qualified: List[StrategyDSL] = []
        for candidate in candidates:
            reason = self.eligible(candidate, target)
            if reason:
                result.rejected.append(
                    {"strategy_id": candidate.strategy_id, "reason": reason}
                )
                continue
            qualified.append(candidate)

        ranked = self.rank(qualified, series)
        ranked = [e for e in ranked if e.sortino >= self.min_sortino or not series]
        admitted, correlation_rejects = self.correlation_filter(ranked, series)
        result.rejected.extend(correlation_rejects)

        for overflow in admitted[self.max_roster :]:
            result.rejected.append(
                {"strategy_id": overflow.strategy.strategy_id, "reason": "ROSTER_CAPACITY"}
            )
        admitted = admitted[: self.max_roster]

        for i, entry in enumerate(admitted, start=1):
            entry.rank = i
            entry.strategy.active = True
        for candidate in candidates:
            if candidate.strategy_id not in {e.strategy.strategy_id for e in admitted}:
                candidate.active = False

        result.roster = self.allocate(admitted, series)
        self.roster = result.roster
        return result

    # ── roster queries ─────────────────────────────────────────────────────
    def active_for_regime(self, regime: "Regime | str") -> List[StrategyDSL]:
        target = regime if isinstance(regime, Regime) else Regime(str(regime).upper())
        return [e.strategy for e in self.roster if e.strategy.is_compatible_with(target)]

    def webhook_triggers(self) -> List[Dict[str, Any]]:
        """Deployment roster in the shape the Pine alert dispatcher consumes."""
        return [
            {
                "strategy_id": e.strategy.strategy_id,
                "symbol": e.strategy.symbol,
                "venue": e.strategy.venue.value,
                "timeframe": e.strategy.timeframe,
                "risk_pct": round(
                    e.strategy.risk_parameters.risk_per_trade_pct * e.weight * len(self.roster), 4
                ),
                "compatible_regimes": [r.value for r in e.strategy.compatible_regimes],
            }
            for e in self.roster
        ]


async def persist_run(result: AllocationResult, agent: str = "orchestrator") -> bool:
    """Write the allocation pass into ``agent_runs`` (best effort)."""
    from app.core import ledger

    return await ledger.record_agent_run(
        run_id=result.run_id,
        agent=agent,
        regime=result.regime.value if result.regime else None,
        candidates_created=len(result.roster) + len(result.rejected),
        candidates_passed=len(result.roster),
        best_dsr=max((e.dsr for e in result.roster), default=None),
        payload=result.to_dict(),
    )
