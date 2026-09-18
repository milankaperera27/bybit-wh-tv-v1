"""Agent 2 — Strategy Ideation (spec §2, LAYER 5).

Genetic synthesis of Smart-Money-Concept primitive trees.  A genome is the
``primitives`` block of the §2.1 DSL: a structure trigger (MSS/BOS), a liquidity
sweep, an FVG/OB entry zone, a trend EMA filter and an order-flow filter.  A
population is evolved with tournament selection, uniform crossover and per-gene
mutation, all seeded so a run is reproducible.

Output artifact: a list of :class:`StrategyDSL` candidates targeting one active
regime, handed straight to Agent 3 for walk-forward validation.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from app.models.schemas import Regime, Venue
from app.models.strategy_dsl import (
    Primitives,
    RiskParameters,
    StrategyDSL,
    TakeProfitLevel,
    ValidationMetrics,
)

# ── SMC primitive vocabulary ───────────────────────────────────────────────
STRUCTURE_TRIGGERS: List[str] = [
    "MSS_CONFIRMED",
    "BOS_CONFIRMED",
    "CHOCH_CONFIRMED",
    "MSS_WITH_DISPLACEMENT",
]

LIQUIDITY_SWEEPS: List[str] = [
    "PREVIOUS_SESSION_LOW",
    "PREVIOUS_SESSION_HIGH",
    "ASIA_RANGE_LOW",
    "ASIA_RANGE_HIGH",
    "EQUAL_LOWS",
    "EQUAL_HIGHS",
    "NONE",
]

ENTRY_ZONES: List[str] = [
    "BULLISH_FVG_50PCT_CONSEQUENT_ENCROACHMENT",
    "BEARISH_FVG_50PCT_CONSEQUENT_ENCROACHMENT",
    "BULLISH_ORDER_BLOCK_OPEN",
    "BEARISH_ORDER_BLOCK_OPEN",
    "BREAKER_BLOCK_RETEST",
    "IMBALANCE_MIDPOINT",
]

TREND_EMAS: List[int] = [20, 50, 100, 200]

VOLUME_FILTERS: List[str] = [
    "CVD_POSITIVE_SLOPE",
    "CVD_NEGATIVE_SLOPE",
    "RELATIVE_VOLUME_GT_1_5",
    "NONE",
]

STOP_TYPES: List[str] = ["SWING_LOW_OFFSET", "SWING_HIGH_OFFSET", "ATR_MULTIPLE", "FVG_EDGE"]

TIMEFRAMES: List[str] = ["1m", "5m", "15m", "1h"]

#: Which primitives are directionally coherent with each regime.
REGIME_BIAS: Dict[Regime, Dict[str, Sequence[str]]] = {
    Regime.BULL_TREND_EXPANSION: {
        "sweep": ["PREVIOUS_SESSION_LOW", "ASIA_RANGE_LOW", "EQUAL_LOWS"],
        "zone": [
            "BULLISH_FVG_50PCT_CONSEQUENT_ENCROACHMENT",
            "BULLISH_ORDER_BLOCK_OPEN",
            "BREAKER_BLOCK_RETEST",
        ],
        "volume": ["CVD_POSITIVE_SLOPE", "RELATIVE_VOLUME_GT_1_5"],
        "stop": ["SWING_LOW_OFFSET", "FVG_EDGE"],
    },
    Regime.BEAR_TREND_EXPANSION: {
        "sweep": ["PREVIOUS_SESSION_HIGH", "ASIA_RANGE_HIGH", "EQUAL_HIGHS"],
        "zone": [
            "BEARISH_FVG_50PCT_CONSEQUENT_ENCROACHMENT",
            "BEARISH_ORDER_BLOCK_OPEN",
            "BREAKER_BLOCK_RETEST",
        ],
        "volume": ["CVD_NEGATIVE_SLOPE", "RELATIVE_VOLUME_GT_1_5"],
        "stop": ["SWING_HIGH_OFFSET", "FVG_EDGE"],
    },
    Regime.RANGE_COMPRESSION: {
        "sweep": ["EQUAL_LOWS", "EQUAL_HIGHS", "ASIA_RANGE_LOW", "ASIA_RANGE_HIGH"],
        "zone": ["IMBALANCE_MIDPOINT", "BULLISH_ORDER_BLOCK_OPEN", "BEARISH_ORDER_BLOCK_OPEN"],
        "volume": ["RELATIVE_VOLUME_GT_1_5", "NONE"],
        "stop": ["ATR_MULTIPLE"],
    },
    Regime.VOLATILITY_CHOP: {
        "sweep": ["NONE", "EQUAL_LOWS", "EQUAL_HIGHS"],
        "zone": ["BREAKER_BLOCK_RETEST", "IMBALANCE_MIDPOINT"],
        "volume": ["NONE", "RELATIVE_VOLUME_GT_1_5"],
        "stop": ["ATR_MULTIPLE"],
    },
}


@dataclass
class Genome:
    """One point in the SMC primitive-tree search space."""

    structure_trigger: str
    liquidity_sweep: str
    entry_zone: str
    filter_trend_ema: int
    volume_filter: str
    stop_loss_type: str
    stop_offset_ticks: int
    risk_per_trade_pct: float
    tp1_ratio: float
    tp2_ratio: float
    tp1_size_pct: float
    breakeven_trigger_r: float
    timeframe: str
    fitness: float = 0.0

    def fingerprint(self) -> str:
        """Stable short hash — used to build the ``strategy_id``."""
        blob = "|".join(
            str(x)
            for x in (
                self.structure_trigger,
                self.liquidity_sweep,
                self.entry_zone,
                self.filter_trend_ema,
                self.volume_filter,
                self.stop_loss_type,
                self.stop_offset_ticks,
                round(self.risk_per_trade_pct, 3),
                round(self.tp1_ratio, 3),
                round(self.tp2_ratio, 3),
                round(self.tp1_size_pct, 1),
                round(self.breakeven_trigger_r, 2),
                self.timeframe,
            )
        )
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:8].upper()

    def to_primitives(self) -> Primitives:
        return Primitives(
            structure_trigger=self.structure_trigger,
            liquidity_sweep=None if self.liquidity_sweep == "NONE" else self.liquidity_sweep,
            entry_zone=self.entry_zone,
            filter_trend_ema=self.filter_trend_ema,
            volume_filter=None if self.volume_filter == "NONE" else self.volume_filter,
        )

    def to_risk_parameters(self, max_contracts: int = 4) -> RiskParameters:
        tp1 = round(min(max(self.tp1_size_pct, 10.0), 90.0), 1)
        return RiskParameters(
            risk_per_trade_pct=round(self.risk_per_trade_pct, 3),
            max_contracts=max_contracts,
            stop_loss_type=self.stop_loss_type,
            stop_offset_ticks=self.stop_offset_ticks,
            take_profit_levels=[
                TakeProfitLevel(ratio=round(self.tp1_ratio, 2), size_pct=tp1),
                TakeProfitLevel(ratio=round(self.tp2_ratio, 2), size_pct=round(100.0 - tp1, 1)),
            ],
            breakeven_trigger_r=round(self.breakeven_trigger_r, 2),
        )


#: A fitness function scores one genome; Agent 3 supplies the real one.
FitnessFn = Callable[[Genome], float]


def heuristic_fitness(genome: Genome, regime: Regime) -> float:
    """Cheap prior used when no backtest callback is supplied.

    Rewards regime-coherent primitives, a sane reward ladder and a reasonable
    risk budget, so the population is already sensible before Agent 3 runs.
    """
    bias = REGIME_BIAS[regime]
    score = 0.0
    score += 1.0 if genome.liquidity_sweep in bias["sweep"] else 0.0
    score += 1.5 if genome.entry_zone in bias["zone"] else 0.0
    score += 0.75 if genome.volume_filter in bias["volume"] else 0.0
    score += 0.75 if genome.stop_loss_type in bias["stop"] else 0.0
    # Reward a ladder that actually ladders.
    score += 0.5 if genome.tp2_ratio > genome.tp1_ratio >= 1.0 else -0.5
    # Penalise oversized risk.
    score -= max(0.0, genome.risk_per_trade_pct - 2.0)
    # Prefer a meaningful trend filter in trending regimes.
    if regime in (Regime.BULL_TREND_EXPANSION, Regime.BEAR_TREND_EXPANSION):
        score += 0.5 if genome.filter_trend_ema >= 100 else 0.0
    else:
        score += 0.25 if genome.filter_trend_ema <= 50 else 0.0
    return score


class IdeationAgent:
    """Genetic synthesiser of SMC primitive trees."""

    def __init__(
        self,
        population_size: int = 24,
        generations: int = 8,
        mutation_rate: float = 0.25,
        crossover_rate: float = 0.7,
        elite_count: int = 2,
        tournament_size: int = 3,
        seed: Optional[int] = 42,
    ) -> None:
        self.population_size = max(4, population_size)
        self.generations = max(1, generations)
        self.mutation_rate = mutation_rate
        self.crossover_rate = crossover_rate
        self.elite_count = max(1, elite_count)
        self.tournament_size = max(2, tournament_size)
        self.rng = random.Random(seed)

    # ── genome construction ────────────────────────────────────────────────
    def random_genome(self, regime: Regime) -> Genome:
        """Sample a genome biased toward the target regime."""
        bias = REGIME_BIAS[regime]
        rng = self.rng
        return Genome(
            structure_trigger=rng.choice(STRUCTURE_TRIGGERS),
            liquidity_sweep=(
                rng.choice(list(bias["sweep"])) if rng.random() < 0.7 else rng.choice(LIQUIDITY_SWEEPS)
            ),
            entry_zone=(
                rng.choice(list(bias["zone"])) if rng.random() < 0.7 else rng.choice(ENTRY_ZONES)
            ),
            filter_trend_ema=rng.choice(TREND_EMAS),
            volume_filter=(
                rng.choice(list(bias["volume"])) if rng.random() < 0.7 else rng.choice(VOLUME_FILTERS)
            ),
            stop_loss_type=(
                rng.choice(list(bias["stop"])) if rng.random() < 0.7 else rng.choice(STOP_TYPES)
            ),
            stop_offset_ticks=rng.randint(2, 12),
            risk_per_trade_pct=round(rng.uniform(0.5, 2.0), 2),
            tp1_ratio=round(rng.uniform(1.0, 2.5), 2),
            tp2_ratio=round(rng.uniform(2.5, 5.0), 2),
            tp1_size_pct=float(rng.choice([25, 33, 50, 60, 75])),
            breakeven_trigger_r=round(rng.uniform(0.5, 1.5), 2),
            timeframe=rng.choice(TIMEFRAMES),
        )

    # ── genetic operators ──────────────────────────────────────────────────
    def crossover(self, a: Genome, b: Genome) -> Genome:
        """Uniform crossover across every gene."""
        rng = self.rng
        pick = lambda x, y: x if rng.random() < 0.5 else y  # noqa: E731
        return Genome(
            structure_trigger=pick(a.structure_trigger, b.structure_trigger),
            liquidity_sweep=pick(a.liquidity_sweep, b.liquidity_sweep),
            entry_zone=pick(a.entry_zone, b.entry_zone),
            filter_trend_ema=pick(a.filter_trend_ema, b.filter_trend_ema),
            volume_filter=pick(a.volume_filter, b.volume_filter),
            stop_loss_type=pick(a.stop_loss_type, b.stop_loss_type),
            stop_offset_ticks=pick(a.stop_offset_ticks, b.stop_offset_ticks),
            risk_per_trade_pct=pick(a.risk_per_trade_pct, b.risk_per_trade_pct),
            tp1_ratio=pick(a.tp1_ratio, b.tp1_ratio),
            tp2_ratio=pick(a.tp2_ratio, b.tp2_ratio),
            tp1_size_pct=pick(a.tp1_size_pct, b.tp1_size_pct),
            breakeven_trigger_r=pick(a.breakeven_trigger_r, b.breakeven_trigger_r),
            timeframe=pick(a.timeframe, b.timeframe),
        )

    def mutate(self, genome: Genome, regime: Regime) -> Genome:
        """Per-gene mutation at ``mutation_rate``."""
        rng = self.rng
        donor = self.random_genome(regime)
        fields = [
            "structure_trigger",
            "liquidity_sweep",
            "entry_zone",
            "filter_trend_ema",
            "volume_filter",
            "stop_loss_type",
            "stop_offset_ticks",
            "risk_per_trade_pct",
            "tp1_ratio",
            "tp2_ratio",
            "tp1_size_pct",
            "breakeven_trigger_r",
            "timeframe",
        ]
        for name in fields:
            if rng.random() < self.mutation_rate:
                setattr(genome, name, getattr(donor, name))
        # Keep the ladder monotonic after mutation.
        if genome.tp2_ratio <= genome.tp1_ratio:
            genome.tp2_ratio = round(genome.tp1_ratio + rng.uniform(0.5, 2.0), 2)
        return genome

    def tournament(self, population: List[Genome]) -> Genome:
        contenders = self.rng.sample(population, min(self.tournament_size, len(population)))
        return max(contenders, key=lambda g: g.fitness)

    # ── evolution ──────────────────────────────────────────────────────────
    def evolve(
        self,
        regime: Regime,
        fitness_fn: Optional[FitnessFn] = None,
    ) -> List[Genome]:
        """Run the GA and return the final population, best first."""
        score = fitness_fn or (lambda g: heuristic_fitness(g, regime))
        population = [self.random_genome(regime) for _ in range(self.population_size)]
        for genome in population:
            genome.fitness = score(genome)

        for _ in range(self.generations):
            population.sort(key=lambda g: g.fitness, reverse=True)
            next_gen: List[Genome] = population[: self.elite_count]
            while len(next_gen) < self.population_size:
                parent_a = self.tournament(population)
                parent_b = self.tournament(population)
                child = (
                    self.crossover(parent_a, parent_b)
                    if self.rng.random() < self.crossover_rate
                    else self.random_genome(regime)
                )
                child = self.mutate(child, regime)
                child.fitness = score(child)
                next_gen.append(child)
            population = next_gen

        population.sort(key=lambda g: g.fitness, reverse=True)
        return population

    # ── DSL emission ───────────────────────────────────────────────────────
    def to_dsl(
        self,
        genome: Genome,
        symbol: str,
        venue: Venue,
        regime: Regime,
        generation: int = 0,
        max_contracts: int = 4,
    ) -> StrategyDSL:
        """Wrap one genome as a §2.1 Strategy DSL document."""
        regime_tag = regime.value.split("_")[0]
        zone_tag = genome.entry_zone.split("_")[1] if "_" in genome.entry_zone else "SMC"
        strategy_id = f"STRAT_{symbol.upper()}_{regime_tag}_{zone_tag}_{genome.fingerprint()}"
        return StrategyDSL(
            strategy_id=strategy_id,
            symbol=symbol,
            venue=venue,
            compatible_regimes=[regime],
            timeframe=genome.timeframe,
            primitives=genome.to_primitives(),
            risk_parameters=genome.to_risk_parameters(max_contracts=max_contracts),
            validation_metrics=ValidationMetrics(),
            generation=generation,
        )

    def generate(
        self,
        regime: "Regime | str",
        symbol: str = "NQ",
        venue: "Venue | str" = Venue.TRADOVATE,
        count: int = 5,
        fitness_fn: Optional[FitnessFn] = None,
        max_contracts: int = 4,
    ) -> List[StrategyDSL]:
        """Evolve, then emit the top ``count`` candidates as StrategyDSL."""
        target = regime if isinstance(regime, Regime) else Regime(str(regime).upper())
        target_venue = venue if isinstance(venue, Venue) else Venue(str(venue).upper())
        population = self.evolve(target, fitness_fn=fitness_fn)

        seen: set[str] = set()
        candidates: List[StrategyDSL] = []
        for genome in population:
            dsl = self.to_dsl(
                genome,
                symbol=symbol,
                venue=target_venue,
                regime=target,
                generation=self.generations,
                max_contracts=max_contracts,
            )
            if dsl.strategy_id in seen:
                continue
            seen.add(dsl.strategy_id)
            candidates.append(dsl)
            if len(candidates) >= count:
                break
        return candidates


def summarise(candidates: List[StrategyDSL]) -> Dict[str, Any]:
    """Compact run summary for the ``agent_runs`` audit row."""
    return {
        "count": len(candidates),
        "strategy_ids": [c.strategy_id for c in candidates],
        "timeframes": sorted({c.timeframe for c in candidates}),
        "entry_zones": sorted({c.primitives.entry_zone or "NONE" for c in candidates}),
    }
