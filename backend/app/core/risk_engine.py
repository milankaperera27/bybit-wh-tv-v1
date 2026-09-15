"""Risk guardrails (LAYER 2).

Pure, side-effect-free functions plus a :class:`RiskDecision` dataclass so the
whole engine is trivially unit-testable without Redis, Postgres or a broker:

* position sizing from ``risk_pct`` × equity ÷ (stop distance × tick value)
* a hard max-contract clamp
* a max-concurrent-positions gate
* a daily drawdown lockout
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from app.core.config import Settings, get_settings

# ── Contract specifications ────────────────────────────────────────────────


@dataclass(frozen=True)
class InstrumentSpec:
    """Tick geometry for one tradable instrument."""

    symbol: str
    tick_size: float
    tick_value: float
    min_qty: float = 1.0
    qty_step: float = 1.0
    fractional: bool = False

    def risk_per_unit(self, stop_distance: float) -> float:
        """Currency risk of one contract/unit for the given stop distance."""
        if self.tick_size <= 0:
            return 0.0
        return (stop_distance / self.tick_size) * self.tick_value


#: CME futures (spec §4.1) — root symbol → tick geometry.
CME_SPECS: Dict[str, InstrumentSpec] = {
    "NQ": InstrumentSpec("NQ", 0.25, 5.00),
    "MNQ": InstrumentSpec("MNQ", 0.25, 0.50),
    "ES": InstrumentSpec("ES", 0.25, 12.50),
    "MES": InstrumentSpec("MES", 0.25, 1.25),
    "YM": InstrumentSpec("YM", 1.00, 5.00),
    "MYM": InstrumentSpec("MYM", 1.00, 0.50),
    "RTY": InstrumentSpec("RTY", 0.10, 5.00),
    "M2K": InstrumentSpec("M2K", 0.10, 0.50),
    "CL": InstrumentSpec("CL", 0.01, 10.00),
    "MCL": InstrumentSpec("MCL", 0.01, 1.00),
    "GC": InstrumentSpec("GC", 0.10, 10.00),
    "MGC": InstrumentSpec("MGC", 0.10, 1.00),
}

#: Kraken margin pairs are quoted/sized fractionally in the base asset.
CRYPTO_SPEC_TEMPLATE = InstrumentSpec(
    symbol="CRYPTO",
    tick_size=0.01,
    tick_value=0.01,
    min_qty=0.0001,
    qty_step=0.0001,
    fractional=True,
)

#: Fallback for an unknown CME root: 1-point tick, $1 per point.
DEFAULT_FUTURES_SPEC = InstrumentSpec("UNKNOWN", 1.00, 1.00)


def normalise_root(symbol: str) -> str:
    """``"NQ1!"`` / ``"NQZ2024"`` / ``"MNQ!"`` → ``"NQ"`` / ``"MNQ"``."""
    token = (symbol or "").strip().upper()
    token = token.replace("!", "")
    # Strip a trailing contract-month/year tail such as "1", "Z4", "Z2024".
    while token and (token[-1].isdigit()):
        token = token[:-1]
    if len(token) > 2 and token[-1] in "FGHJKMNQUVXZ" and token[:-1] in CME_SPECS:
        token = token[:-1]
    return token or (symbol or "").strip().upper()


def resolve_instrument(symbol: str, venue: str = "TRADOVATE") -> InstrumentSpec:
    """Return the :class:`InstrumentSpec` for ``symbol`` on ``venue``."""
    if (venue or "").upper() == "KRAKEN":
        return InstrumentSpec(
            symbol=(symbol or "CRYPTO").upper(),
            tick_size=CRYPTO_SPEC_TEMPLATE.tick_size,
            tick_value=CRYPTO_SPEC_TEMPLATE.tick_value,
            min_qty=CRYPTO_SPEC_TEMPLATE.min_qty,
            qty_step=CRYPTO_SPEC_TEMPLATE.qty_step,
            fractional=True,
        )
    root = normalise_root(symbol)
    spec = CME_SPECS.get(root)
    if spec is not None:
        return spec
    return InstrumentSpec(
        symbol=root or (symbol or "UNKNOWN"),
        tick_size=DEFAULT_FUTURES_SPEC.tick_size,
        tick_value=DEFAULT_FUTURES_SPEC.tick_value,
    )


# ── Decision object ────────────────────────────────────────────────────────


@dataclass
class RiskDecision:
    """Verdict of the risk engine for one candidate order."""

    approved: bool
    reason: str
    qty: float
    risk_amount: float = 0.0
    stop_distance: float = 0.0
    risk_per_unit: float = 0.0
    uncapped_qty: float = 0.0
    clamped: bool = False
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "approved": self.approved,
            "reason": self.reason,
            "qty": self.qty,
            "risk_amount": round(self.risk_amount, 4),
            "stop_distance": round(self.stop_distance, 8),
            "risk_per_unit": round(self.risk_per_unit, 6),
            "uncapped_qty": self.uncapped_qty,
            "clamped": self.clamped,
            "details": self.details,
        }


# ── Pure helpers ───────────────────────────────────────────────────────────


def stop_distance(entry_price: float, stop_loss: float) -> float:
    """Absolute price distance between entry and protective stop."""
    return abs(float(entry_price) - float(stop_loss))


def risk_amount(equity: float, risk_pct: float) -> float:
    """Currency at risk for one trade."""
    return float(equity) * (float(risk_pct) / 100.0)


def clamp_quantity(qty: float, spec: InstrumentSpec, max_contracts: int) -> float:
    """Round to the instrument step then apply the hard max-contract clamp."""
    limit = float(max_contracts)
    if spec.fractional:
        step = spec.qty_step or 0.0001
        stepped = math.floor(float(qty) / step) * step
        stepped = round(stepped, 8)
    else:
        stepped = float(math.floor(float(qty)))
    return min(stepped, limit)


def position_size(
    equity: float,
    risk_pct: float,
    entry_price: float,
    stop_loss: float,
    spec: InstrumentSpec,
    max_contracts: int,
) -> RiskDecision:
    """Core sizing routine: risk budget ÷ per-unit risk, then clamped."""
    distance = stop_distance(entry_price, stop_loss)
    budget = risk_amount(equity, risk_pct)

    if distance <= 0:
        return RiskDecision(
            approved=False,
            reason="INVALID_STOP: stop_loss must differ from the entry price",
            qty=0.0,
            risk_amount=budget,
            stop_distance=distance,
        )

    per_unit = spec.risk_per_unit(distance)
    if per_unit <= 0:
        return RiskDecision(
            approved=False,
            reason="INVALID_INSTRUMENT: non-positive risk per unit",
            qty=0.0,
            risk_amount=budget,
            stop_distance=distance,
        )

    raw_qty = budget / per_unit
    qty = clamp_quantity(raw_qty, spec, max_contracts)
    clamped = qty < (
        round(math.floor(raw_qty / (spec.qty_step or 1.0)) * (spec.qty_step or 1.0), 8)
        if spec.fractional
        else math.floor(raw_qty)
    )

    if qty < spec.min_qty or qty <= 0:
        return RiskDecision(
            approved=False,
            reason=(
                "SIZE_BELOW_MINIMUM: risk budget "
                f"{budget:.2f} cannot fund one unit at {per_unit:.2f} risk"
            ),
            qty=0.0,
            risk_amount=budget,
            stop_distance=distance,
            risk_per_unit=per_unit,
            uncapped_qty=raw_qty,
        )

    return RiskDecision(
        approved=True,
        reason="OK",
        qty=qty,
        risk_amount=budget,
        stop_distance=distance,
        risk_per_unit=per_unit,
        uncapped_qty=raw_qty,
        clamped=clamped,
    )


def drawdown_breached(
    daily_pnl: float, equity: float, max_daily_drawdown_pct: float
) -> bool:
    """True once realised daily loss meets or exceeds the lockout threshold."""
    if equity <= 0:
        return True
    if daily_pnl >= 0:
        return False
    loss_pct = abs(float(daily_pnl)) / float(equity) * 100.0
    return loss_pct >= float(max_daily_drawdown_pct)


def concurrency_breached(open_positions: int, max_concurrent: int) -> bool:
    return int(open_positions) >= int(max_concurrent)


def clamp_risk_pct(requested: float, settings: Settings) -> float:
    """Never honour a webhook asking for more than ``ATLAS_MAX_RISK_PCT``."""
    value = float(requested if requested and requested > 0 else settings.default_risk_pct)
    return min(value, float(settings.max_risk_pct))


# ── Full evaluation ────────────────────────────────────────────────────────


def evaluate(
    *,
    symbol: str,
    venue: str,
    entry_price: float,
    stop_loss: float,
    risk_pct: float,
    equity: Optional[float] = None,
    daily_pnl: float = 0.0,
    open_positions: int = 0,
    settings: Optional[Settings] = None,
    kill_switch_engaged: bool = False,
) -> RiskDecision:
    """Run every guardrail in order and return a single verdict.

    Order matters: kill switch → drawdown lockout → concurrency → sizing.  The
    first breach short-circuits with ``approved=False`` and a machine-readable
    ``reason`` prefix.
    """
    cfg = settings or get_settings()
    account_equity = float(equity if equity is not None else cfg.account_equity)
    spec = resolve_instrument(symbol, venue)
    effective_risk_pct = clamp_risk_pct(risk_pct, cfg)

    base_details: Dict[str, Any] = {
        "symbol": symbol,
        "venue": venue,
        "instrument": spec.symbol,
        "tick_size": spec.tick_size,
        "tick_value": spec.tick_value,
        "equity": account_equity,
        "requested_risk_pct": risk_pct,
        "effective_risk_pct": effective_risk_pct,
        "daily_pnl": daily_pnl,
        "open_positions": open_positions,
        "max_contracts": cfg.max_contracts,
        "max_concurrent_positions": cfg.max_concurrent_positions,
        "max_daily_drawdown_pct": cfg.max_daily_drawdown_pct,
    }

    if kill_switch_engaged:
        return RiskDecision(
            approved=False,
            reason="KILL_SWITCH_ENGAGED: global trading halt is active",
            qty=0.0,
            details=base_details,
        )

    if drawdown_breached(daily_pnl, account_equity, cfg.max_daily_drawdown_pct):
        return RiskDecision(
            approved=False,
            reason=(
                "DAILY_DRAWDOWN_LOCKOUT: realised "
                f"{daily_pnl:.2f} breaches the {cfg.max_daily_drawdown_pct}% daily limit"
            ),
            qty=0.0,
            details=base_details,
        )

    if concurrency_breached(open_positions, cfg.max_concurrent_positions):
        return RiskDecision(
            approved=False,
            reason=(
                "MAX_CONCURRENT_POSITIONS: "
                f"{open_positions} open >= limit {cfg.max_concurrent_positions}"
            ),
            qty=0.0,
            details=base_details,
        )

    decision = position_size(
        equity=account_equity,
        risk_pct=effective_risk_pct,
        entry_price=entry_price,
        stop_loss=stop_loss,
        spec=spec,
        max_contracts=cfg.max_contracts,
    )
    decision.details = {**base_details, **decision.details}
    return decision
