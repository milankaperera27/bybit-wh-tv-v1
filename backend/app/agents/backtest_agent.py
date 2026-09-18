"""Agent 3 — Validation Engine (spec §2, LAYER 5).

Four stages, numpy/pandas only:

1. **Vectorised backtest** — signal → position → per-bar returns, with slippage
   and commission charged on every position change.
2. **Walk-forward optimisation** — rolling in-sample/out-of-sample folds; the
   parameter chosen in-sample is the one scored out-of-sample.
3. **Monte Carlo** — 1,000 bootstrap draws over the trade sequence, yielding
   equity confidence intervals.
4. **Deflated Sharpe Ratio** — Bailey & López de Prado's multiple-testing
   correction.  Candidates are gated at ``DSR >= 1.5``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from app.models.strategy_dsl import StrategyDSL, ValidationMetrics

DSR_GATE = 1.5
DEFAULT_MC_DRAWS = 1000
TRADING_PERIODS_PER_YEAR = 252.0

#: Euler–Mascheroni constant, used by the expected-maximum-Sharpe estimator.
EULER_GAMMA = 0.5772156649015329


# ── Stage 1: vectorised backtest ───────────────────────────────────────────
@dataclass
class BacktestResult:
    """Per-run statistics of a vectorised backtest."""

    returns: np.ndarray
    equity: np.ndarray
    trade_returns: np.ndarray
    sharpe: float
    sortino: float
    max_drawdown_pct: float
    profit_factor: float
    win_rate: float
    trade_count: int
    total_return_pct: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sharpe": round(self.sharpe, 4),
            "sortino": round(self.sortino, 4),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "profit_factor": round(self.profit_factor, 4),
            "win_rate": round(self.win_rate, 4),
            "trade_count": self.trade_count,
            "total_return_pct": round(self.total_return_pct, 4),
        }


def sharpe_ratio(returns: np.ndarray, periods_per_year: float = TRADING_PERIODS_PER_YEAR) -> float:
    """Annualised Sharpe of a per-period return series."""
    r = np.asarray(returns, dtype=float)
    if r.size < 2:
        return 0.0
    sd = float(np.std(r, ddof=1))
    if sd == 0.0:
        return 0.0
    return float(np.mean(r) / sd * math.sqrt(periods_per_year))


def sortino_ratio(
    returns: np.ndarray, periods_per_year: float = TRADING_PERIODS_PER_YEAR
) -> float:
    """Annualised Sortino — downside deviation in the denominator."""
    r = np.asarray(returns, dtype=float)
    if r.size < 2:
        return 0.0
    downside = r[r < 0]
    if downside.size == 0:
        return float(np.mean(r) * math.sqrt(periods_per_year) / 1e-9) if np.mean(r) > 0 else 0.0
    dd = float(np.sqrt(np.mean(np.square(downside))))
    if dd == 0.0:
        return 0.0
    return float(np.mean(r) / dd * math.sqrt(periods_per_year))


def max_drawdown_pct(equity: np.ndarray) -> float:
    """Peak-to-trough drawdown of an equity curve, in percent."""
    eq = np.asarray(equity, dtype=float)
    if eq.size == 0:
        return 0.0
    peak = np.maximum.accumulate(eq)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peak > 0, (eq - peak) / peak, 0.0)
    return float(abs(np.min(dd)) * 100.0)


def profit_factor(trade_returns: np.ndarray) -> float:
    """Gross profit ÷ gross loss."""
    t = np.asarray(trade_returns, dtype=float)
    gains = float(t[t > 0].sum())
    losses = float(-t[t < 0].sum())
    if losses == 0.0:
        return float("inf") if gains > 0 else 0.0
    return gains / losses


def extract_trade_returns(positions: np.ndarray, returns: np.ndarray) -> np.ndarray:
    """Collapse per-bar returns into one compounded return per position leg."""
    pos = np.asarray(positions, dtype=float)
    ret = np.asarray(returns, dtype=float)
    trades: List[float] = []
    current: Optional[float] = None
    accumulator = 1.0
    for i in range(pos.size):
        state = pos[i]
        if state != current:
            if current is not None and current != 0.0:
                trades.append(accumulator - 1.0)
            current = state
            accumulator = 1.0
        if state != 0.0:
            accumulator *= 1.0 + ret[i]
    if current is not None and current != 0.0:
        trades.append(accumulator - 1.0)
    return np.asarray(trades, dtype=float)


def vectorised_backtest(
    prices: Sequence[float],
    signals: Sequence[float],
    *,
    slippage_bps: float = 1.0,
    commission_bps: float = 0.5,
    periods_per_year: float = TRADING_PERIODS_PER_YEAR,
) -> BacktestResult:
    """Run one vectorised pass.

    ``signals[i]`` is the desired position for bar ``i`` (+1 long, -1 short,
    0 flat).  The signal is lagged by one bar so no look-ahead leaks in, and
    transaction costs are charged on every change of position.
    """
    px = np.asarray(prices, dtype=float)
    sig = np.asarray(signals, dtype=float)
    if px.size < 2 or sig.size != px.size:
        empty = np.zeros(0)
        return BacktestResult(empty, np.ones(1), empty, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0.0)

    bar_returns = np.zeros_like(px)
    bar_returns[1:] = np.diff(px) / np.where(px[:-1] == 0, np.nan, px[:-1])
    bar_returns = np.nan_to_num(bar_returns, nan=0.0, posinf=0.0, neginf=0.0)

    positions = np.roll(sig, 1)
    positions[0] = 0.0

    gross = positions * bar_returns
    turnover = np.abs(np.diff(positions, prepend=0.0))
    cost = turnover * ((slippage_bps + commission_bps) / 10_000.0)
    net = gross - cost

    equity = np.cumprod(1.0 + net)
    trade_returns = extract_trade_returns(positions, bar_returns - cost)
    wins = int((trade_returns > 0).sum()) if trade_returns.size else 0

    return BacktestResult(
        returns=net,
        equity=equity,
        trade_returns=trade_returns,
        sharpe=sharpe_ratio(net, periods_per_year),
        sortino=sortino_ratio(net, periods_per_year),
        max_drawdown_pct=max_drawdown_pct(equity),
        profit_factor=profit_factor(trade_returns),
        win_rate=(wins / trade_returns.size) if trade_returns.size else 0.0,
        trade_count=int(trade_returns.size),
        total_return_pct=float((equity[-1] - 1.0) * 100.0) if equity.size else 0.0,
    )


# ── Stage 2: walk-forward optimisation ─────────────────────────────────────
@dataclass
class WalkForwardFold:
    index: int
    train_slice: Tuple[int, int]
    test_slice: Tuple[int, int]
    chosen_params: Dict[str, Any]
    in_sample_sharpe: float
    out_sample_sharpe: float


@dataclass
class WalkForwardResult:
    folds: List[WalkForwardFold] = field(default_factory=list)
    oos_returns: np.ndarray = field(default_factory=lambda: np.zeros(0))

    @property
    def in_sample_sharpe(self) -> float:
        return float(np.mean([f.in_sample_sharpe for f in self.folds])) if self.folds else 0.0

    @property
    def out_sample_sharpe(self) -> float:
        return float(np.mean([f.out_sample_sharpe for f in self.folds])) if self.folds else 0.0

    @property
    def trials(self) -> int:
        """Total parameter evaluations — the DSR's multiple-testing count."""
        return max(1, len(self.folds))


#: A signal generator maps (prices, params) → a position series.
SignalFn = Callable[[np.ndarray, Dict[str, Any]], np.ndarray]


def walk_forward(
    prices: Sequence[float],
    signal_fn: SignalFn,
    param_grid: List[Dict[str, Any]],
    *,
    folds: int = 4,
    train_ratio: float = 0.7,
    slippage_bps: float = 1.0,
    commission_bps: float = 0.5,
) -> WalkForwardResult:
    """Rolling WFO: fit in-sample, score the winner out-of-sample."""
    px = np.asarray(prices, dtype=float)
    result = WalkForwardResult()
    if px.size < folds * 20 or not param_grid:
        return result

    window = px.size // folds
    train_len = max(10, int(window * train_ratio))
    oos_chunks: List[np.ndarray] = []

    for i in range(folds):
        start = i * window
        end = min(px.size, start + window)
        if end - start < train_len + 5:
            continue
        train = px[start : start + train_len]
        test = px[start + train_len : end]

        best_params: Dict[str, Any] = param_grid[0]
        best_sharpe = -np.inf
        for params in param_grid:
            run = vectorised_backtest(
                train,
                signal_fn(train, params),
                slippage_bps=slippage_bps,
                commission_bps=commission_bps,
            )
            if run.sharpe > best_sharpe:
                best_sharpe, best_params = run.sharpe, params

        oos = vectorised_backtest(
            test,
            signal_fn(test, best_params),
            slippage_bps=slippage_bps,
            commission_bps=commission_bps,
        )
        oos_chunks.append(oos.returns)
        result.folds.append(
            WalkForwardFold(
                index=i,
                train_slice=(start, start + train_len),
                test_slice=(start + train_len, end),
                chosen_params=dict(best_params),
                in_sample_sharpe=float(best_sharpe),
                out_sample_sharpe=oos.sharpe,
            )
        )

    if oos_chunks:
        result.oos_returns = np.concatenate(oos_chunks)
    return result


# ── Stage 3: Monte Carlo ───────────────────────────────────────────────────
@dataclass
class MonteCarloResult:
    draws: int
    mean_return_pct: float
    p05_return_pct: float
    p50_return_pct: float
    p95_return_pct: float
    p95_max_drawdown_pct: float
    prob_profit: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "draws": self.draws,
            "mean_return_pct": round(self.mean_return_pct, 4),
            "p05_return_pct": round(self.p05_return_pct, 4),
            "p50_return_pct": round(self.p50_return_pct, 4),
            "p95_return_pct": round(self.p95_return_pct, 4),
            "p95_max_drawdown_pct": round(self.p95_max_drawdown_pct, 4),
            "prob_profit": round(self.prob_profit, 4),
        }


def monte_carlo(
    trade_returns: Sequence[float],
    draws: int = DEFAULT_MC_DRAWS,
    seed: Optional[int] = 7,
) -> MonteCarloResult:
    """Bootstrap the trade sequence ``draws`` times for equity confidence bands."""
    t = np.asarray(trade_returns, dtype=float)
    if t.size == 0:
        return MonteCarloResult(draws, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    rng = np.random.default_rng(seed)
    sample = rng.choice(t, size=(draws, t.size), replace=True)
    curves = np.cumprod(1.0 + sample, axis=1)
    finals = (curves[:, -1] - 1.0) * 100.0

    peaks = np.maximum.accumulate(curves, axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peaks > 0, (curves - peaks) / peaks, 0.0)
    worst_dd = np.abs(dd.min(axis=1)) * 100.0

    return MonteCarloResult(
        draws=draws,
        mean_return_pct=float(np.mean(finals)),
        p05_return_pct=float(np.percentile(finals, 5)),
        p50_return_pct=float(np.percentile(finals, 50)),
        p95_return_pct=float(np.percentile(finals, 95)),
        p95_max_drawdown_pct=float(np.percentile(worst_dd, 95)),
        prob_profit=float((finals > 0).mean()),
    )


# ── Stage 4: Deflated Sharpe Ratio ─────────────────────────────────────────
def expected_max_sharpe(trials: int, variance_of_sharpes: float = 1.0) -> float:
    """E[max SR] over ``trials`` independent strategies (Bailey & LdP, 2014)."""
    n = max(2, int(trials))
    sd = math.sqrt(max(variance_of_sharpes, 1e-12))
    z1 = _norm_ppf(1.0 - 1.0 / n)
    z2 = _norm_ppf(1.0 - 1.0 / (n * math.e))
    return sd * ((1.0 - EULER_GAMMA) * z1 + EULER_GAMMA * z2)


def deflated_sharpe_ratio(
    returns: Sequence[float],
    trials: int = 1,
    variance_of_sharpes: float = 1.0,
    periods_per_year: float = TRADING_PERIODS_PER_YEAR,
) -> float:
    """Multiple-testing-corrected Sharpe, expressed as a z-score.

    The observed Sharpe is deflated by ``E[max SR]`` across ``trials`` and then
    standardised by the Sharpe's own standard error (which accounts for the
    return distribution's skew and kurtosis).  Higher is better; the production
    gate is ``>= 1.5``.
    """
    r = np.asarray(returns, dtype=float)
    n = r.size
    if n < 8:
        return 0.0
    sd = float(np.std(r, ddof=1))
    if sd == 0.0:
        return 0.0

    # Per-period (non-annualised) Sharpe — the DSR algebra assumes this scale.
    sr = float(np.mean(r) / sd)
    centred = (r - np.mean(r)) / sd
    skew = float(np.mean(centred**3))
    kurt = float(np.mean(centred**4))

    sr_star = expected_max_sharpe(trials, variance_of_sharpes) / math.sqrt(periods_per_year)

    denom_sq = 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr**2
    if denom_sq <= 0:
        return 0.0
    denominator = math.sqrt(denom_sq / (n - 1))
    if denominator == 0:
        return 0.0
    return float((sr - sr_star) / denominator)


def _norm_ppf(p: float) -> float:
    """Inverse standard-normal CDF (Acklam's rational approximation)."""
    p = min(max(p, 1e-12), 1 - 1e-12)
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


# ── Orchestration ──────────────────────────────────────────────────────────
def default_signal_fn(prices: np.ndarray, params: Dict[str, Any]) -> np.ndarray:
    """Reference generator: an EMA-cross proxy for the DSL's trend filter."""
    from app.agents.regime_agent import ema

    fast = ema(prices, int(params.get("fast", 20)))
    slow = ema(prices, int(params.get("slow", 50)))
    return np.where(fast > slow, 1.0, -1.0 if params.get("allow_short", True) else 0.0)


@dataclass
class ValidationReport:
    """Everything Agent 3 hands to Agent 4."""

    strategy_id: str
    passed: bool
    metrics: ValidationMetrics
    backtest: Dict[str, Any]
    walk_forward: Dict[str, Any]
    monte_carlo: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "passed": self.passed,
            "metrics": self.metrics.model_dump(),
            "backtest": self.backtest,
            "walk_forward": self.walk_forward,
            "monte_carlo": self.monte_carlo,
        }


class BacktestAgent:
    """Vectorised backtest + WFO + Monte Carlo + DSR gate."""

    def __init__(
        self,
        dsr_threshold: float = DSR_GATE,
        monte_carlo_draws: int = DEFAULT_MC_DRAWS,
        slippage_bps: float = 1.0,
        commission_bps: float = 0.5,
        periods_per_year: float = TRADING_PERIODS_PER_YEAR,
        seed: Optional[int] = 7,
    ) -> None:
        self.dsr_threshold = dsr_threshold
        self.monte_carlo_draws = monte_carlo_draws
        self.slippage_bps = slippage_bps
        self.commission_bps = commission_bps
        self.periods_per_year = periods_per_year
        self.seed = seed

    def default_param_grid(self, candidate: StrategyDSL) -> List[Dict[str, Any]]:
        """Grid seeded from the candidate's own trend-EMA primitive."""
        base = int(candidate.primitives.filter_trend_ema or 50)
        return [
            {"fast": f, "slow": s}
            for f in (10, 20, 50)
            for s in (base, base * 2)
            if f < s
        ] or [{"fast": 20, "slow": 50}]

    def validate(
        self,
        candidate: StrategyDSL,
        prices: Sequence[float],
        signal_fn: Optional[SignalFn] = None,
        param_grid: Optional[List[Dict[str, Any]]] = None,
        folds: int = 4,
    ) -> ValidationReport:
        """Run all four stages and stamp ``validation_metrics`` on the candidate."""
        fn = signal_fn or default_signal_fn
        grid = param_grid or self.default_param_grid(candidate)
        px = np.asarray(prices, dtype=float)

        wfo = walk_forward(
            px,
            fn,
            grid,
            folds=folds,
            slippage_bps=self.slippage_bps,
            commission_bps=self.commission_bps,
        )

        best_params = wfo.folds[-1].chosen_params if wfo.folds else grid[0]
        full = vectorised_backtest(
            px,
            fn(px, best_params),
            slippage_bps=self.slippage_bps,
            commission_bps=self.commission_bps,
            periods_per_year=self.periods_per_year,
        )

        oos = wfo.oos_returns if wfo.oos_returns.size >= 8 else full.returns
        trials = max(1, wfo.trials * len(grid))
        dsr = deflated_sharpe_ratio(
            oos, trials=trials, periods_per_year=self.periods_per_year
        )
        mc = monte_carlo(full.trade_returns, draws=self.monte_carlo_draws, seed=self.seed)

        metrics = ValidationMetrics(
            in_sample_sharpe=round(wfo.in_sample_sharpe or full.sharpe, 4),
            out_sample_sharpe=round(wfo.out_sample_sharpe or full.sharpe, 4),
            deflated_sharpe_ratio=round(dsr, 4),
            max_drawdown_pct=round(full.max_drawdown_pct, 4),
            profit_factor=round(
                full.profit_factor if math.isfinite(full.profit_factor) else 999.0, 4
            ),
            trade_count=full.trade_count,
            monte_carlo_draws=mc.draws,
            mc_p05_return_pct=round(mc.p05_return_pct, 4),
            mc_p95_return_pct=round(mc.p95_return_pct, 4),
            sortino_ratio=round(full.sortino, 4),
        )
        candidate.validation_metrics = metrics

        return ValidationReport(
            strategy_id=candidate.strategy_id,
            passed=metrics.passes(self.dsr_threshold),
            metrics=metrics,
            backtest=full.to_dict(),
            walk_forward={
                "folds": len(wfo.folds),
                "trials": trials,
                "in_sample_sharpe": round(wfo.in_sample_sharpe, 4),
                "out_sample_sharpe": round(wfo.out_sample_sharpe, 4),
                "chosen_params": best_params,
            },
            monte_carlo=mc.to_dict(),
        )

    def gate(
        self,
        candidates: List[StrategyDSL],
        prices: Sequence[float],
        signal_fn: Optional[SignalFn] = None,
    ) -> Tuple[List[StrategyDSL], List[ValidationReport]]:
        """Validate every candidate; return only those clearing the DSR gate."""
        reports = [self.validate(c, prices, signal_fn=signal_fn) for c in candidates]
        survivors = [
            c for c, r in zip(candidates, reports) if r.passed
        ]
        return survivors, reports
