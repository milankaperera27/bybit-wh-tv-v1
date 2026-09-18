"""Acceptance gate §6.4 — risk guardrails.

Sizing arithmetic is asserted against hand-computed CME tick geometry, so a
regression in the tick tables or the budget maths fails here rather than in
production.
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.core.risk_engine import (
    CME_SPECS,
    clamp_quantity,
    clamp_risk_pct,
    concurrency_breached,
    drawdown_breached,
    evaluate,
    position_size,
    resolve_instrument,
    risk_amount,
    stop_distance,
)

EQUITY = 100_000.0


class TestTickGeometry:
    def test_nq_risk_per_unit_is_20_dollars_per_point(self) -> None:
        """NQ: 0.25 tick, $5.00/tick → 4 ticks/point → $20/point."""
        assert CME_SPECS["NQ"].risk_per_unit(1.0) == pytest.approx(20.0)

    def test_mnq_is_one_tenth_of_nq(self) -> None:
        assert CME_SPECS["MNQ"].risk_per_unit(1.0) == pytest.approx(2.0)

    def test_es_risk_per_point(self) -> None:
        assert CME_SPECS["ES"].risk_per_unit(1.0) == pytest.approx(50.0)

    @pytest.mark.parametrize("symbol,root", [("NQZ5", "NQ"), ("MESH6", "MES"), ("NQ1!", "NQ")])
    def test_contract_months_resolve_to_their_root(self, symbol: str, root: str) -> None:
        assert resolve_instrument(symbol, "TRADOVATE").symbol == root

    def test_kraken_pairs_are_fractional(self) -> None:
        assert resolve_instrument("XBTUSD", "KRAKEN").fractional is True


class TestSizingArithmetic:
    def test_budget_is_equity_times_risk_pct(self) -> None:
        assert risk_amount(EQUITY, 1.25) == pytest.approx(1250.0)

    def test_stop_distance_is_absolute(self) -> None:
        assert stop_distance(20140.0, 20155.25) == pytest.approx(15.25)

    def test_nq_position_size_is_hand_checkable(self) -> None:
        """$1,000 budget ÷ (15.25pt × $20/pt = $305) = 3.27 → floor 3."""
        decision = position_size(
            equity=EQUITY, risk_pct=1.0,
            entry_price=20155.25, stop_loss=20140.00,
            spec=CME_SPECS["NQ"], max_contracts=4,
        )
        assert decision.approved
        assert decision.risk_amount == pytest.approx(1000.0)
        assert decision.risk_per_unit == pytest.approx(305.0)
        assert decision.uncapped_qty == pytest.approx(1000.0 / 305.0)
        assert decision.qty == 3

    def test_quantity_is_floored_never_rounded_up(self) -> None:
        """Rounding up would risk more than the stated budget."""
        decision = position_size(
            equity=EQUITY, risk_pct=1.0,
            entry_price=20160.0, stop_loss=20140.0,   # 20pt × $20 = $400/contract
            spec=CME_SPECS["NQ"], max_contracts=10,
        )
        assert decision.uncapped_qty == pytest.approx(2.5)
        assert decision.qty == 2

    def test_realised_risk_never_exceeds_the_budget(self) -> None:
        decision = position_size(
            equity=EQUITY, risk_pct=1.0,
            entry_price=20155.25, stop_loss=20140.00,
            spec=CME_SPECS["NQ"], max_contracts=4,
        )
        assert decision.qty * decision.risk_per_unit <= decision.risk_amount

    def test_zero_stop_distance_is_refused(self) -> None:
        decision = position_size(
            equity=EQUITY, risk_pct=1.0,
            entry_price=20155.25, stop_loss=20155.25,
            spec=CME_SPECS["NQ"], max_contracts=4,
        )
        assert not decision.approved
        assert decision.reason.startswith("INVALID_STOP")
        assert decision.qty == 0.0

    def test_budget_too_small_for_one_contract(self) -> None:
        decision = position_size(
            equity=1_000.0, risk_pct=0.1,             # $1 budget
            entry_price=20155.25, stop_loss=20140.00,
            spec=CME_SPECS["NQ"], max_contracts=4,
        )
        assert not decision.approved
        assert decision.reason.startswith("SIZE_BELOW_MINIMUM")


class TestContractClamp:
    def test_max_contract_clamp_binds(self) -> None:
        decision = position_size(
            equity=10_000_000.0, risk_pct=5.0,
            entry_price=20155.25, stop_loss=20140.00,
            spec=CME_SPECS["NQ"], max_contracts=4,
        )
        assert decision.approved
        assert decision.qty == 4
        assert decision.clamped is True

    def test_unclamped_size_is_not_flagged(self) -> None:
        decision = position_size(
            equity=EQUITY, risk_pct=1.0,
            entry_price=20155.25, stop_loss=20140.00,
            spec=CME_SPECS["NQ"], max_contracts=4,
        )
        assert decision.clamped is False

    def test_clamp_quantity_floors_whole_contracts(self) -> None:
        assert clamp_quantity(3.9, CME_SPECS["NQ"], 10) == 3
        assert clamp_quantity(99.0, CME_SPECS["NQ"], 4) == 4


class TestRiskPctCeiling:
    def test_webhook_cannot_exceed_the_configured_maximum(self) -> None:
        settings = get_settings()
        assert clamp_risk_pct(50.0, settings) == pytest.approx(settings.max_risk_pct)

    def test_zero_or_missing_falls_back_to_the_default(self) -> None:
        settings = get_settings()
        assert clamp_risk_pct(0.0, settings) == pytest.approx(settings.default_risk_pct)


class TestDrawdownLockout:
    @pytest.mark.parametrize(
        "daily_pnl,expected",
        [(0.0, False), (5_000.0, False), (-2_900.0, False), (-3_000.0, True), (-9_000.0, True)],
    )
    def test_three_percent_daily_lockout(self, daily_pnl: float, expected: bool) -> None:
        assert drawdown_breached(daily_pnl, EQUITY, 3.0) is expected

    def test_threshold_is_inclusive(self) -> None:
        assert drawdown_breached(-3_000.0, EQUITY, 3.0) is True

    def test_zero_equity_is_always_breached(self) -> None:
        assert drawdown_breached(0.0, 0.0, 3.0) is True


class TestConcurrencyCap:
    @pytest.mark.parametrize("open_positions,expected", [(0, False), (2, False), (3, True), (5, True)])
    def test_open_position_cap(self, open_positions: int, expected: bool) -> None:
        assert concurrency_breached(open_positions, 3) is expected


class TestEvaluateOrdering:
    """Guardrails must short-circuit in order: kill → drawdown → concurrency → size."""

    def _evaluate(self, **overrides):
        kwargs = dict(
            symbol="NQZ5", venue="TRADOVATE",
            entry_price=20155.25, stop_loss=20140.00,
            risk_pct=1.0, equity=EQUITY,
        )
        kwargs.update(overrides)
        return evaluate(**kwargs)

    def test_happy_path_is_approved(self) -> None:
        decision = self._evaluate()
        assert decision.approved
        assert decision.qty == 3
        assert decision.reason == "OK"

    def test_kill_switch_wins_over_everything(self) -> None:
        decision = self._evaluate(kill_switch_engaged=True, daily_pnl=-50_000.0, open_positions=99)
        assert not decision.approved
        assert decision.reason.startswith("KILL_SWITCH_ENGAGED")

    def test_drawdown_precedes_concurrency(self) -> None:
        decision = self._evaluate(daily_pnl=-4_000.0, open_positions=99)
        assert not decision.approved
        assert decision.reason.startswith("DAILY_DRAWDOWN_LOCKOUT")

    def test_concurrency_precedes_sizing(self) -> None:
        decision = self._evaluate(open_positions=3)
        assert not decision.approved
        assert decision.reason.startswith("MAX_CONCURRENT_POSITIONS")

    def test_rejections_always_size_to_zero(self) -> None:
        for decision in (
            self._evaluate(kill_switch_engaged=True),
            self._evaluate(daily_pnl=-4_000.0),
            self._evaluate(open_positions=3),
        ):
            assert decision.qty == 0.0

    def test_excessive_requested_risk_is_capped_not_honoured(self) -> None:
        decision = self._evaluate(risk_pct=99.0)
        assert decision.details["effective_risk_pct"] == pytest.approx(get_settings().max_risk_pct)

    def test_decision_serialises_for_the_audit_ledger(self) -> None:
        payload = self._evaluate().to_dict()
        assert payload["approved"] is True and payload["qty"] == 3
