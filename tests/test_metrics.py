"""Performance metrics."""

import math

import pytest

from tsl.backtest.costs import OrderKind, Side
from tsl.backtest.engine import TakeProfit, TradePlan, TradeResult
from tsl.backtest.metrics import summarise

from datetime import datetime, timedelta, timezone

UTC = timezone.utc
START = datetime(2024, 1, 8, 7, tzinfo=UTC)


def _result(r, entered=True, outcome="stopped", ambiguous=False):
    plan = TradePlan(
        symbol="TEST", side=Side.LONG, created_at=START, entry_kind=OrderKind.MARKET,
        entry_price=100.0, stop_loss=99.0,
        take_profits=(TakeProfit(2.0, 1.0),),
        entry_expiry=START + timedelta(minutes=30),
        close_at=START + timedelta(hours=8),
        lots=0.05, value_per_price_unit=10.0,
    )
    out = TradeResult(plan=plan, entered=entered, outcome=outcome, ambiguous_bar=ambiguous)
    out.r_multiple = r
    out.pnl = r * plan.risk_amount
    return out


def test_no_trades_is_reported_not_crashed():
    assert summarise([]).trades == 0
    assert str(summarise([])) == "no trades"


def test_expectancy_is_the_mean_outcome():
    results = [_result(2.0), _result(-1.0), _result(-1.0), _result(2.0)]
    p = summarise(results)
    assert p.trades == 4
    assert p.expectancy_r == pytest.approx(0.5)
    assert p.total_r == pytest.approx(2.0)
    assert p.total_pnl == pytest.approx(20.0)


def test_a_losing_majority_can_still_be_profitable():
    """Why win rate alone is meaningless: 33% wins at 3R beats 67% losses at 1R."""
    results = [_result(3.0)] + [_result(-1.0)] * 2
    p = summarise(results)
    assert p.win_rate == pytest.approx(1 / 3)
    assert p.expectancy_r > 0
    assert p.payoff_ratio == pytest.approx(3.0)


def test_untriggered_trades_are_counted_but_not_averaged():
    """A strategy that rarely fires is a different problem from one that loses."""
    results = [_result(2.0), _result(0.0, entered=False, outcome="expired")]
    p = summarise(results)
    assert p.trades == 1
    assert p.expectancy_r == pytest.approx(2.0)
    assert p.outcomes["expired"] == 1


def test_sample_size_gates_the_verdict():
    """+0.4R over 10 trades says almost nothing, and must not read as a result."""
    small = summarise([_result(2.0), _result(-1.0)] * 5)
    assert small.trades == 10
    assert not small.expectancy_confident
    assert "NOT distinguishable from luck" in str(small)


def test_a_large_consistent_sample_is_distinguishable():
    p = summarise([_result(2.0), _result(-1.0), _result(2.0), _result(-1.0)] * 30)
    assert p.trades == 120
    assert p.expectancy_confident


def test_standard_error_shrinks_with_more_trades():
    few = summarise([_result(2.0), _result(-1.0)] * 10)
    many = summarise([_result(2.0), _result(-1.0)] * 200)
    assert many.expectancy_stderr < few.expectancy_stderr
    assert many.expectancy_stderr == pytest.approx(
        few.expectancy_stderr / math.sqrt(20), rel=0.05
    )


def test_drawdown_measures_peak_to_trough():
    # +3R, then four consecutive losses.
    results = [_result(3.0)] + [_result(-1.0)] * 4
    p = summarise(results)
    assert p.max_drawdown_r == pytest.approx(4.0)
    assert p.longest_losing_streak == 4


def test_drawdown_from_an_underwater_start():
    p = summarise([_result(-1.0), _result(-1.0), _result(3.0)])
    assert p.max_drawdown_r == pytest.approx(2.0)


def test_profit_factor_is_gross_win_over_gross_loss():
    p = summarise([_result(2.0), _result(2.0), _result(-1.0)])
    assert p.profit_factor == pytest.approx(4.0)


def test_ambiguous_trades_are_surfaced_not_buried():
    """If the result leans on bars the data cannot resolve, that must be visible."""
    results = [_result(-1.0, ambiguous=True)] * 3 + [_result(2.0)] * 7
    p = summarise(results)
    assert p.ambiguous_trades == 3
    assert p.ambiguous_share == pytest.approx(0.3)
    assert "ambiguous bars" in str(p)
    assert "the result" in str(p)


def test_no_ambiguity_means_no_warning():
    assert "ambiguous" not in str(summarise([_result(2.0)] * 10))


def test_summary_leads_with_expectancy_and_sample_size():
    text = str(summarise([_result(2.0), _result(-1.0)] * 25))
    lines = [line for line in text.splitlines() if line.strip()]
    assert "trades" in lines[0]
    assert "expectancy" in lines[1]
    assert "win rate" in text and "max drawdown" in text
