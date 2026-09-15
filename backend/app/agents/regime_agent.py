"""Agent 1 — Regime Classifier (spec §2, LAYER 5).

Four features → four states, then an HMM-style transition smoother so the
classification does not flicker bar-to-bar:

* **ATR percentile** — expansion vs. compression of realised range
* **ADX**            — trend strength
* **EMA stack**      — fast/slow alignment and price side
* **CVD slope**      — cumulative volume delta order-flow bias

Emits ``BULL_TREND_EXPANSION`` / ``BEAR_TREND_EXPANSION`` /
``RANGE_COMPRESSION`` / ``VOLATILITY_CHOP``.  numpy only — no pandas, no hmmlearn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np

from app.models.schemas import Regime

STATES: List[Regime] = [
    Regime.BULL_TREND_EXPANSION,
    Regime.BEAR_TREND_EXPANSION,
    Regime.RANGE_COMPRESSION,
    Regime.VOLATILITY_CHOP,
]

#: Row-stochastic 4-state transition matrix.  Regimes are sticky (0.80 on the
#: diagonal); a direct bull→bear flip is the least likely move.
TRANSITION_MATRIX = np.array(
    [
        # to:   BULL   BEAR   RANGE   CHOP
        [0.80, 0.02, 0.09, 0.09],  # from BULL
        [0.02, 0.80, 0.09, 0.09],  # from BEAR
        [0.10, 0.10, 0.70, 0.10],  # from RANGE
        [0.12, 0.12, 0.16, 0.60],  # from CHOP
    ],
    dtype=float,
)

DEFAULT_ADX_THRESHOLD = 22.0
DEFAULT_ATR_EXPANSION_PCTILE = 60.0
DEFAULT_ATR_COMPRESSION_PCTILE = 40.0


# ── Indicator primitives (numpy) ───────────────────────────────────────────
def ema(values: Sequence[float], length: int) -> np.ndarray:
    """Exponential moving average, seeded with the first observation."""
    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        return arr
    alpha = 2.0 / (length + 1.0)
    out = np.empty_like(arr)
    out[0] = arr[0]
    for i in range(1, arr.size):
        out[i] = alpha * arr[i] + (1.0 - alpha) * out[i - 1]
    return out


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    prev_close = np.concatenate(([close[0]], close[:-1]))
    return np.maximum.reduce(
        [high - low, np.abs(high - prev_close), np.abs(low - prev_close)]
    )


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, length: int = 14) -> np.ndarray:
    """Wilder's ATR via an EMA of the true range."""
    return ema(true_range(high, low, close), length)


def adx(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, length: int = 14
) -> np.ndarray:
    """Wilder's ADX (directional movement index)."""
    n = close.size
    if n < 2:
        return np.zeros(n)
    up_move = np.diff(high, prepend=high[0])
    down_move = -np.diff(low, prepend=low[0])
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr_smooth = ema(true_range(high, low, close), length)
    plus_di = 100.0 * _safe_divide(ema(plus_dm, length), tr_smooth)
    minus_di = 100.0 * _safe_divide(ema(minus_dm, length), tr_smooth)
    dx = 100.0 * _safe_divide(np.abs(plus_di - minus_di), plus_di + minus_di)
    return ema(dx, length)


def _safe_divide(numerator: np.ndarray, denominator: np.ndarray) -> np.ndarray:
    """Element-wise divide where a zero denominator yields 0.0, not NaN/inf."""
    out = np.zeros_like(numerator, dtype=float)
    np.divide(numerator, denominator, out=out, where=denominator != 0)
    return np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0)


def percentile_rank(series: np.ndarray, window: int = 100) -> np.ndarray:
    """Rolling percentile rank (0-100) of each value within its trailing window."""
    n = series.size
    out = np.zeros(n)
    for i in range(n):
        start = max(0, i - window + 1)
        window_slice = series[start : i + 1]
        if window_slice.size <= 1:
            out[i] = 50.0
        else:
            out[i] = 100.0 * float((window_slice <= series[i]).sum() - 1) / (
                window_slice.size - 1
            )
    return out


def cumulative_volume_delta(
    close: np.ndarray, open_: np.ndarray, volume: np.ndarray
) -> np.ndarray:
    """Bar-signed CVD proxy: volume signed by the candle's direction."""
    sign = np.sign(close - open_)
    sign[sign == 0] = 0.0
    return np.cumsum(sign * volume)


def slope(series: np.ndarray, window: int = 20) -> float:
    """Least-squares slope of the last ``window`` observations, scale-normalised."""
    if series.size < 2:
        return 0.0
    tail = series[-min(window, series.size) :]
    x = np.arange(tail.size, dtype=float)
    raw = float(np.polyfit(x, tail, 1)[0])
    scale = float(np.std(tail)) or 1.0
    return raw / scale


# ── Emission model ─────────────────────────────────────────────────────────
@dataclass
class RegimeFeatures:
    """The four inputs the classifier scores."""

    atr_percentile: float
    adx: float
    ema_fast: float
    ema_slow: float
    close: float
    cvd_slope: float

    @property
    def ema_stack_bullish(self) -> bool:
        return self.ema_fast > self.ema_slow and self.close > self.ema_slow

    @property
    def ema_stack_bearish(self) -> bool:
        return self.ema_fast < self.ema_slow and self.close < self.ema_slow


@dataclass
class RegimeResult:
    """One classification, smoothed and with its full posterior."""

    regime: Regime
    confidence: float
    probabilities: Dict[str, float]
    features: RegimeFeatures
    raw_regime: Regime
    history: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "regime": self.regime.value,
            "raw_regime": self.raw_regime.value,
            "confidence": round(self.confidence, 4),
            "probabilities": {k: round(v, 4) for k, v in self.probabilities.items()},
            "features": {
                "atr_percentile": round(self.features.atr_percentile, 2),
                "adx": round(self.features.adx, 2),
                "ema_fast": round(self.features.ema_fast, 4),
                "ema_slow": round(self.features.ema_slow, 4),
                "close": round(self.features.close, 4),
                "cvd_slope": round(self.features.cvd_slope, 4),
            },
        }


def emission_scores(
    features: RegimeFeatures,
    adx_threshold: float = DEFAULT_ADX_THRESHOLD,
    expansion_pctile: float = DEFAULT_ATR_EXPANSION_PCTILE,
    compression_pctile: float = DEFAULT_ATR_COMPRESSION_PCTILE,
) -> np.ndarray:
    """Un-normalised likelihood of each state given the observed features."""
    trending = _sigmoid((features.adx - adx_threshold) / 6.0)
    expanding = _sigmoid((features.atr_percentile - expansion_pctile) / 12.0)
    compressing = _sigmoid((compression_pctile - features.atr_percentile) / 12.0)
    bullish = 1.0 if features.ema_stack_bullish else 0.0
    bearish = 1.0 if features.ema_stack_bearish else 0.0
    flow_up = _sigmoid(features.cvd_slope * 4.0)
    flow_down = 1.0 - flow_up

    bull = trending * expanding * (0.25 + 0.75 * bullish) * (0.35 + 0.65 * flow_up)
    bear = trending * expanding * (0.25 + 0.75 * bearish) * (0.35 + 0.65 * flow_down)
    rng = (1.0 - trending) * compressing
    chop = (1.0 - trending) * (1.0 - compressing) + trending * (1.0 - expanding) * 0.6

    scores = np.array([bull, bear, rng, chop], dtype=float)
    scores = np.clip(scores, 1e-9, None)
    return scores / scores.sum()


def _sigmoid(x: float) -> float:
    return float(1.0 / (1.0 + np.exp(-np.clip(x, -30.0, 30.0))))


class RegimeAgent:
    """Stateful 4-state classifier with an HMM-style forward smoother."""

    def __init__(
        self,
        atr_length: int = 14,
        adx_length: int = 14,
        ema_fast_length: int = 50,
        ema_slow_length: int = 200,
        percentile_window: int = 100,
        cvd_window: int = 20,
        adx_threshold: float = DEFAULT_ADX_THRESHOLD,
        transition_matrix: Optional[np.ndarray] = None,
    ) -> None:
        self.atr_length = atr_length
        self.adx_length = adx_length
        self.ema_fast_length = ema_fast_length
        self.ema_slow_length = ema_slow_length
        self.percentile_window = percentile_window
        self.cvd_window = cvd_window
        self.adx_threshold = adx_threshold
        self.transition_matrix = (
            transition_matrix if transition_matrix is not None else TRANSITION_MATRIX
        )
        # Uniform prior until the first observation arrives.
        self.belief = np.full(len(STATES), 1.0 / len(STATES))
        self.history: List[str] = []

    # ── feature extraction ─────────────────────────────────────────────────
    def extract_features(
        self,
        high: Sequence[float],
        low: Sequence[float],
        close: Sequence[float],
        volume: Optional[Sequence[float]] = None,
        open_: Optional[Sequence[float]] = None,
    ) -> RegimeFeatures:
        h = np.asarray(high, dtype=float)
        l = np.asarray(low, dtype=float)
        c = np.asarray(close, dtype=float)
        if c.size == 0:
            raise ValueError("close series is empty")
        o = np.asarray(open_, dtype=float) if open_ is not None else np.concatenate(([c[0]], c[:-1]))
        v = np.asarray(volume, dtype=float) if volume is not None else np.ones_like(c)

        atr_series = atr(h, l, c, self.atr_length)
        atr_pct = percentile_rank(atr_series, self.percentile_window)[-1]
        adx_series = adx(h, l, c, self.adx_length)
        fast = ema(c, self.ema_fast_length)
        slow = ema(c, self.ema_slow_length)
        cvd = cumulative_volume_delta(c, o, v)

        return RegimeFeatures(
            atr_percentile=float(atr_pct),
            adx=float(adx_series[-1]),
            ema_fast=float(fast[-1]),
            ema_slow=float(slow[-1]),
            close=float(c[-1]),
            cvd_slope=slope(cvd, self.cvd_window),
        )

    # ── HMM-style smoothing ────────────────────────────────────────────────
    def step(self, features: RegimeFeatures) -> RegimeResult:
        """Advance the belief one bar: predict via A, correct via the emission."""
        emissions = emission_scores(features, adx_threshold=self.adx_threshold)
        predicted = self.belief @ self.transition_matrix
        posterior = predicted * emissions
        total = posterior.sum()
        posterior = posterior / total if total > 0 else np.full(len(STATES), 1.0 / len(STATES))
        self.belief = posterior

        smoothed_idx = int(np.argmax(posterior))
        raw_idx = int(np.argmax(emissions))
        regime = STATES[smoothed_idx]
        self.history.append(regime.value)

        return RegimeResult(
            regime=regime,
            confidence=float(posterior[smoothed_idx]),
            probabilities={STATES[i].value: float(posterior[i]) for i in range(len(STATES))},
            features=features,
            raw_regime=STATES[raw_idx],
            history=list(self.history[-32:]),
        )

    def classify(
        self,
        high: Sequence[float],
        low: Sequence[float],
        close: Sequence[float],
        volume: Optional[Sequence[float]] = None,
        open_: Optional[Sequence[float]] = None,
    ) -> RegimeResult:
        """Classify the most recent bar of the supplied series."""
        return self.step(self.extract_features(high, low, close, volume, open_))

    def classify_series(
        self,
        high: Sequence[float],
        low: Sequence[float],
        close: Sequence[float],
        volume: Optional[Sequence[float]] = None,
        open_: Optional[Sequence[float]] = None,
        warmup: int = 50,
    ) -> List[RegimeResult]:
        """Walk the whole series bar-by-bar, returning one result per bar."""
        c = np.asarray(close, dtype=float)
        results: List[RegimeResult] = []
        for i in range(warmup, c.size):
            results.append(
                self.classify(
                    high[: i + 1],
                    low[: i + 1],
                    close[: i + 1],
                    None if volume is None else volume[: i + 1],
                    None if open_ is None else open_[: i + 1],
                )
            )
        return results

    def reset(self) -> None:
        self.belief = np.full(len(STATES), 1.0 / len(STATES))
        self.history.clear()


async def publish_regime(result: RegimeResult) -> bool:
    """Write the current regime to Redis for the dashboard hub."""
    from app.core import redis_client

    try:
        return await redis_client.set_current_regime(result.regime.value)
    except Exception:
        return False
