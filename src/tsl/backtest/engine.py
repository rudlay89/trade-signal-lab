"""Trade execution simulation.

Walks a plan forward through 1-minute bars and records what would actually have
happened, including the parts that are inconvenient.

THE INTRABAR PROBLEM, which decides whether a backtest is honest.

A 1-minute bar reports only open, high, low and close. When a bar's range covers
both the stop-loss and a take-profit, the bar cannot say which was reached first.
The difference is a full loss versus a win, and on a volatile day it happens
often enough to dominate the result.

Assuming the favourable order is how backtests manufacture edges that evaporate
in live trading. This engine always assumes the UNFAVOURABLE one: if both were
touched in the same minute, the stop went first. That understates performance
slightly, which is the correct direction to be wrong in. Trades where it had to
make that call are counted and reported, so their influence is visible rather
than buried - if a strategy's results hinge on many of them, the result is not
trustworthy at any timeframe.

Bars are labelled by their opening instant, so a strategy may only act on a bar
once it has closed. The engine never reads a bar the plan could not have seen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from .costs import CostModel, OrderKind, Side


@dataclass(frozen=True)
class TakeProfit:
    r_multiple: float
    close_fraction: float          # of the ORIGINAL position
    move_stop_to_breakeven: bool = False


@dataclass(frozen=True)
class TradePlan:
    """A fully specified trade, as a strategy emits it."""

    symbol: str
    side: Side
    created_at: datetime           # the close of the bar that produced it
    entry_kind: OrderKind
    entry_price: float
    stop_loss: float
    take_profits: tuple[TakeProfit, ...]
    entry_expiry: datetime         # unfilled orders are cancelled here
    close_at: datetime             # hard time exit
    lots: float
    value_per_price_unit: float    # account currency per 1.0 of price, for `lots`
    strategy: str = ""
    rationale: str = ""

    @property
    def stop_distance(self) -> float:
        return abs(self.entry_price - self.stop_loss)

    @property
    def risk_amount(self) -> float:
        """Account-currency risk if the stop fills at its level. This is 1R."""
        return self.stop_distance * self.value_per_price_unit

    def target_price(self, r_multiple: float) -> float:
        """A target measured in R from the PLANNED entry - where a real trader
        would place the order, before knowing what the fill turned out to be."""
        return self.entry_price + self.side.sign * r_multiple * self.stop_distance


@dataclass
class Fill:
    at: datetime
    price: float
    fraction: float                # of the original position
    reason: str                    # "entry" | "stop" | "tp1" ... | "time_exit"


@dataclass
class TradeResult:
    plan: TradePlan
    fills: list[Fill] = field(default_factory=list)
    pnl: float = 0.0
    r_multiple: float = 0.0
    entered: bool = False
    outcome: str = "not_triggered"
    ambiguous_bar: bool = False    # stop and target both touched in one minute
    bars_held: int = 0

    @property
    def entry_fill(self) -> Fill | None:
        return next((f for f in self.fills if f.reason == "entry"), None)


def simulate_trade(
    plan: TradePlan,
    bars: pd.DataFrame,
    costs: CostModel,
) -> TradeResult:
    """Run one plan through 1-minute bars.

    `bars` must be 1-minute mid-price bars with a `spread_mean` column, covering
    from `plan.created_at` to at least `plan.close_at`.
    """
    result = TradeResult(plan=plan)

    # A plan may only act on bars that open at or after the bar which produced
    # it has closed. Anything earlier is information it did not have.
    window = bars[(bars.index >= plan.created_at) & (bars.index <= plan.close_at)]
    if window.empty:
        result.outcome = "no_data"
        return result

    sign = plan.side.sign
    stop = plan.stop_loss
    remaining = 1.0
    entry_price: float | None = None
    hit_targets: set[int] = set()

    for timestamp, bar in window.iterrows():
        spread = bar.get("spread_mean")

        # --- waiting for entry -------------------------------------------
        if entry_price is None:
            if timestamp > plan.entry_expiry:
                result.outcome = "expired"
                return result

            triggered, raw_fill = _entry_triggered(plan, bar, sign)
            if not triggered:
                continue

            entry_price = costs.fill_price(
                raw_fill, plan.side, plan.entry_kind, spread, closing=False
            )
            result.fills.append(Fill(timestamp, entry_price, 1.0, "entry"))
            result.entered = True
            # A position opened on this bar is not also managed on it: the entry
            # happened somewhere inside the minute, so the bar's extremes are not
            # all available to it. Management starts on the next bar.
            continue

        result.bars_held += 1

        # --- in position --------------------------------------------------
        stop_touched = _touched(bar, stop, against=plan.side)
        next_target = _next_target(plan, hit_targets)
        target_touched = (
            next_target is not None
            and _touched(bar, plan.target_price(next_target.r_multiple), against=plan.side.opposite)
        )

        if stop_touched and target_touched:
            # The bar cannot say which came first. Assume the loss.
            result.ambiguous_bar = True

        if stop_touched:
            price = costs.fill_price(stop, plan.side, OrderKind.STOP, spread, closing=True)
            # A bar that gapped through the stop fills at the open, not the level.
            gap = bar["open"]
            if (sign > 0 and gap < stop) or (sign < 0 and gap > stop):
                price = costs.fill_price(gap, plan.side, OrderKind.STOP, spread, closing=True)
            result.fills.append(Fill(timestamp, price, remaining, "stop"))
            _finish(result, plan, entry_price, remaining, price)
            result.outcome = "stopped" if not hit_targets else "stopped_after_partial"
            return result

        if target_touched:
            level = plan.target_price(next_target.r_multiple)
            price = costs.fill_price(level, plan.side, OrderKind.LIMIT, spread, closing=True)
            fraction = min(next_target.close_fraction, remaining)
            index = plan.take_profits.index(next_target)
            result.fills.append(Fill(timestamp, price, fraction, f"tp{index + 1}"))
            _finish(result, plan, entry_price, fraction, price)

            remaining -= fraction
            hit_targets.add(index)
            if next_target.move_stop_to_breakeven:
                stop = entry_price

            if remaining <= 1e-9:
                result.outcome = "target"
                return result

    # --- time exit --------------------------------------------------------
    if entry_price is None:
        result.outcome = "not_triggered"
        return result

    last = window.iloc[-1]
    price = costs.fill_price(
        last["close"], plan.side, OrderKind.MARKET, last.get("spread_mean"), closing=True
    )
    result.fills.append(Fill(window.index[-1], price, remaining, "time_exit"))
    _finish(result, plan, entry_price, remaining, price)
    result.outcome = "time_exit"
    return result


def _entry_triggered(plan: TradePlan, bar, sign: int) -> tuple[bool, float]:
    """Has the entry order filled on this bar, and at what mid price?"""
    if plan.entry_kind is OrderKind.MARKET:
        return True, bar["open"]

    level = plan.entry_price
    if plan.entry_kind is OrderKind.STOP:
        # A buy stop triggers when price rises through the level.
        reached = bar["high"] >= level if sign > 0 else bar["low"] <= level
        if not reached:
            return False, 0.0
        # If the bar opened beyond the level the fill is at the open, which is
        # worse. Pretending otherwise is free money the market never offered.
        opened_beyond = bar["open"] > level if sign > 0 else bar["open"] < level
        return True, bar["open"] if opened_beyond else level

    # LIMIT: a buy limit sits below the market and fills when price falls to it.
    reached = bar["low"] <= level if sign > 0 else bar["high"] >= level
    if not reached:
        return False, 0.0
    opened_beyond = bar["open"] < level if sign > 0 else bar["open"] > level
    return True, bar["open"] if opened_beyond else level


def _touched(bar, level: float, against: Side) -> bool:
    """Did this bar reach `level`, moving in the direction `against` implies?

    `against=LONG` asks whether price fell to the level (a long's stop);
    `against=SHORT` asks whether it rose to it.
    """
    return bar["low"] <= level if against is Side.LONG else bar["high"] >= level


def _next_target(plan: TradePlan, hit: set[int]) -> TakeProfit | None:
    for index, target in enumerate(plan.take_profits):
        if index not in hit:
            return target
    return None


def _finish(result: TradeResult, plan: TradePlan, entry: float, fraction: float, exit_price: float) -> None:
    """Book the profit or loss on a closed portion of the position."""
    move = (exit_price - entry) * plan.side.sign
    pnl = move * plan.value_per_price_unit * fraction
    result.pnl += pnl
    result.r_multiple = result.pnl / plan.risk_amount if plan.risk_amount else 0.0
