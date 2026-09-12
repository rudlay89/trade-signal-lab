"""Trade execution simulation.

The tests that matter most are the ones about being pessimistic: a backtest that
resolves ambiguity in its own favour will show an edge that does not exist.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from tsl.backtest.costs import CostModel, OrderKind, Side
from tsl.backtest.engine import TakeProfit, TradePlan, simulate_trade

UTC = timezone.utc
START = datetime(2024, 1, 8, 7, 0, tzinfo=UTC)
FREE = CostModel(slippage_spreads=0.0, fallback_spread=0.0)   # frictionless, for arithmetic


def _bars(rows, start=START):
    """Build 1-minute bars from (open, high, low, close) tuples."""
    index = pd.date_range(start, periods=len(rows), freq="1min", tz="UTC")
    frame = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=index)
    frame["spread_mean"] = 0.0
    frame.index.name = "timestamp"
    return frame


def _plan(side=Side.LONG, entry=100.0, stop=99.0, kind=OrderKind.MARKET,
          targets=None, minutes=60, value_per_price_unit=10.0):
    return TradePlan(
        symbol="TEST", side=side, created_at=START, entry_kind=kind,
        entry_price=entry, stop_loss=stop,
        take_profits=targets if targets is not None else (
            TakeProfit(2.0, 0.5, move_stop_to_breakeven=True),
            TakeProfit(3.0, 1.0),
        ),
        entry_expiry=START + timedelta(minutes=30),
        close_at=START + timedelta(minutes=minutes),
        lots=0.05, value_per_price_unit=value_per_price_unit,
    )


# --- the plan's own arithmetic ----------------------------------------------


def test_risk_and_targets_derive_from_the_planned_entry():
    plan = _plan()
    assert plan.stop_distance == pytest.approx(1.0)
    assert plan.risk_amount == pytest.approx(10.0)
    assert plan.target_price(2.0) == pytest.approx(102.0)
    assert plan.target_price(3.0) == pytest.approx(103.0)


def test_short_targets_sit_below_entry():
    plan = _plan(side=Side.SHORT, entry=100.0, stop=101.0)
    assert plan.target_price(2.0) == pytest.approx(98.0)


# --- entry -------------------------------------------------------------------


def test_market_entry_fills_at_the_next_open():
    bars = _bars([(100.0, 100.2, 99.9, 100.1)] * 5)
    result = simulate_trade(_plan(), bars, FREE)
    assert result.entered
    assert result.entry_fill.price == pytest.approx(100.0)


def test_buy_stop_waits_for_the_level():
    bars = _bars([
        (99.0, 99.5, 98.8, 99.2),     # below the trigger
        (99.2, 100.4, 99.1, 100.3),   # reaches it
        (100.3, 100.5, 100.1, 100.4),
    ])
    result = simulate_trade(_plan(kind=OrderKind.STOP, entry=100.0), bars, FREE)
    assert result.entered
    assert result.entry_fill.at == bars.index[1]
    assert result.entry_fill.price == pytest.approx(100.0)


def test_a_gap_through_the_trigger_fills_at_the_open_not_the_level():
    """Pretending otherwise is free money the market never offered."""
    bars = _bars([
        (99.0, 99.5, 98.8, 99.2),
        (101.5, 102.0, 101.0, 101.8),   # opened well above the 100.0 trigger
    ])
    result = simulate_trade(_plan(kind=OrderKind.STOP, entry=100.0), bars, FREE)
    assert result.entry_fill.price == pytest.approx(101.5)


def test_an_untouched_order_expires():
    bars = _bars([(99.0, 99.5, 98.8, 99.2)] * 45)
    result = simulate_trade(_plan(kind=OrderKind.STOP, entry=100.0), bars, FREE)
    assert not result.entered
    assert result.outcome == "expired"


def test_bars_before_the_plan_existed_are_never_read():
    """Acting on data the strategy could not have seen is look-ahead bias."""
    early = _bars([(105.0, 106.0, 104.0, 105.5)] * 3, start=START - timedelta(minutes=3))
    later = _bars([(100.0, 100.2, 99.9, 100.1)] * 3)
    result = simulate_trade(_plan(), pd.concat([early, later]), FREE)
    assert result.entry_fill.price == pytest.approx(100.0)


# --- exits -------------------------------------------------------------------


def test_a_stop_ends_the_trade_at_minus_one_r():
    bars = _bars([
        (100.0, 100.1, 99.9, 100.0),
        (100.0, 100.1, 98.5, 98.8),     # through the 99.0 stop
    ])
    result = simulate_trade(_plan(), bars, FREE)
    assert result.outcome == "stopped"
    assert result.r_multiple == pytest.approx(-1.0)
    assert result.pnl == pytest.approx(-10.0)


def test_partial_at_two_r_then_the_rest_at_three():
    bars = _bars([
        (100.0, 100.1, 99.9, 100.0),
        (100.0, 102.1, 99.9, 102.0),    # 2R touched
        (102.0, 103.2, 101.9, 103.1),   # 3R touched
    ])
    result = simulate_trade(_plan(), bars, FREE)
    assert result.outcome == "target"
    assert [f.reason for f in result.fills] == ["entry", "tp1", "tp2"]
    # Half at +2R and half at +3R is +2.5R.
    assert result.r_multiple == pytest.approx(2.5)


def test_breakeven_stop_engages_after_the_first_target():
    bars = _bars([
        (100.0, 100.1, 99.9, 100.0),
        (100.0, 102.1, 99.9, 102.0),    # 2R hit, stop moves to entry
        (102.0, 102.1, 99.8, 99.9),     # back through entry - but not to 99.0
    ])
    result = simulate_trade(_plan(), bars, FREE)
    assert result.outcome == "stopped_after_partial"
    # +1R on the half that ran, nothing on the half closed at breakeven.
    assert result.r_multiple == pytest.approx(1.0)


def test_time_exit_closes_whatever_remains():
    bars = _bars([(100.0, 100.6, 99.9, 100.5)] * 6, start=START)
    plan = _plan(minutes=5)
    result = simulate_trade(plan, bars, FREE)
    assert result.outcome == "time_exit"
    assert result.r_multiple == pytest.approx(0.5)


def test_a_gap_through_the_stop_fills_at_the_open():
    bars = _bars([
        (100.0, 100.1, 99.9, 100.0),
        (97.0, 97.2, 96.5, 96.8),       # opened below the 99.0 stop
    ])
    result = simulate_trade(_plan(), bars, FREE)
    assert result.fills[-1].price == pytest.approx(97.0)
    assert result.r_multiple == pytest.approx(-3.0)


# --- the honesty tests -------------------------------------------------------


def test_an_ambiguous_bar_is_resolved_as_a_loss():
    """THE CENTRAL RULE. A bar covering both the stop and the target cannot say
    which came first. Assuming the win is how backtests invent edges."""
    bars = _bars([
        (100.0, 100.1, 99.9, 100.0),
        (100.0, 102.5, 98.5, 100.0),    # range covers both 99.0 stop and 102.0 target
    ])
    result = simulate_trade(_plan(), bars, FREE)
    assert result.outcome == "stopped"
    assert result.r_multiple == pytest.approx(-1.0)
    assert result.ambiguous_bar, "the call must be recorded, not made silently"


def test_unambiguous_bars_are_not_flagged():
    bars = _bars([
        (100.0, 100.1, 99.9, 100.0),
        (100.0, 102.1, 99.5, 102.0),    # target only
        (102.0, 103.2, 101.9, 103.1),
    ])
    result = simulate_trade(_plan(), bars, FREE)
    assert not result.ambiguous_bar


def test_the_entry_bar_is_not_also_managed():
    """Entry happens somewhere inside the minute, so that bar's extremes were not
    all available to the position. Using them would be look-ahead."""
    bars = _bars([
        (100.0, 103.5, 98.0, 100.0),    # entry bar, range covers target and stop
        (100.0, 100.1, 99.95, 100.0),
    ])
    result = simulate_trade(_plan(minutes=1), bars, FREE)
    assert result.outcome == "time_exit"
    assert not result.ambiguous_bar


# --- costs bite --------------------------------------------------------------


def test_spread_and_slippage_make_a_losing_trade_worse():
    costs = CostModel(slippage_spreads=0.5, fallback_spread=0.0)
    bars = _bars([
        (100.0, 100.1, 99.9, 100.0),
        (100.0, 100.1, 98.5, 98.8),
    ])
    bars["spread_mean"] = 0.10
    result = simulate_trade(_plan(), bars, costs)

    # Entry at the ask, stop exit at the bid with slippage: worse than -1R.
    assert result.r_multiple < -1.0
    assert result.entry_fill.price == pytest.approx(100.05)


def test_a_wider_spread_costs_more():
    bars = _bars([(100.0, 100.1, 99.9, 100.0)] * 3, start=START)
    costs = CostModel(slippage_spreads=0.5, fallback_spread=0.0)

    results = []
    for spread in (0.02, 0.50):
        frame = bars.copy()
        frame["spread_mean"] = spread
        results.append(simulate_trade(_plan(minutes=2), frame, costs).r_multiple)

    assert results[1] < results[0], "a wider spread must cost more"


def test_the_stored_spread_is_used_not_a_constant():
    """A year of gold ran from 0.38 median to 5.98 max. One number cannot do."""
    bars = _bars([(100.0, 100.1, 99.9, 100.0)] * 3)
    bars["spread_mean"] = [0.02, 2.00, 2.00]
    costs = CostModel(slippage_spreads=0.0, fallback_spread=0.10)

    result = simulate_trade(_plan(minutes=2), bars, costs)
    assert result.entry_fill.price == pytest.approx(100.01)   # the 0.02 bar, not the fallback


def test_a_missing_spread_falls_back_rather_than_costing_nothing():
    bars = _bars([(100.0, 100.1, 99.9, 100.0)] * 3)
    bars["spread_mean"] = float("nan")
    costs = CostModel(slippage_spreads=0.0, fallback_spread=0.40)
    result = simulate_trade(_plan(minutes=2), bars, costs)
    assert result.entry_fill.price == pytest.approx(100.20)


# --- shorts mirror longs -----------------------------------------------------


def test_a_short_stops_out_on_a_rise():
    bars = _bars([
        (100.0, 100.1, 99.9, 100.0),
        (100.0, 101.5, 99.9, 101.2),
    ])
    result = simulate_trade(_plan(side=Side.SHORT, entry=100.0, stop=101.0), bars, FREE)
    assert result.outcome == "stopped"
    assert result.r_multiple == pytest.approx(-1.0)


def test_a_short_reaches_its_targets_on_a_fall():
    bars = _bars([
        (100.0, 100.1, 99.9, 100.0),
        (100.0, 100.1, 97.9, 98.0),
        (98.0, 98.1, 96.9, 97.0),
    ])
    result = simulate_trade(_plan(side=Side.SHORT, entry=100.0, stop=101.0), bars, FREE)
    assert result.outcome == "target"
    assert result.r_multiple == pytest.approx(2.5)


def test_no_data_is_reported_not_crashed():
    empty = _bars([(100.0, 100.1, 99.9, 100.0)], start=START + timedelta(days=5))
    result = simulate_trade(_plan(), empty, FREE)
    assert result.outcome == "no_data"
    assert not result.entered
