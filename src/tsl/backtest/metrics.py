"""Performance metrics.

Chosen to make a strategy prove itself rather than flatter it. Three principles:

EXPECTANCY OVER WIN RATE. Win rate alone is meaningless - a strategy taking 3R
targets will lose most of its trades and still make money. The headline number is
expectancy in R: the average outcome per trade. Win rate and payoff are reported
beside it because they are not independent, and reading either alone misleads.

SAMPLE SIZE IS A RESULT, NOT A FOOTNOTE. An expectancy of +0.4R over 30 trades
says almost nothing. Every summary carries its trade count and a standard error,
so the number is always read next to how much it rests on.

DRAWDOWN IS WHAT ENDS ACCOUNTS. The equity curve's worst peak-to-trough fall, in
R and in account terms, sits alongside the returns - not below the fold.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class Performance:
    """A strategy's results, with the caveats attached rather than implied."""

    trades: int = 0
    wins: int = 0
    losses: int = 0
    scratches: int = 0

    total_r: float = 0.0
    expectancy_r: float = 0.0
    expectancy_stderr: float = 0.0
    total_pnl: float = 0.0

    win_rate: float = 0.0
    average_win_r: float = 0.0
    average_loss_r: float = 0.0
    payoff_ratio: float = 0.0
    profit_factor: float = 0.0

    max_drawdown_r: float = 0.0
    max_drawdown_pct: float = 0.0
    longest_losing_streak: int = 0

    sharpe: float = 0.0

    ambiguous_trades: int = 0
    outcomes: dict = field(default_factory=dict)
    r_series: list = field(default_factory=list)

    @property
    def ambiguous_share(self) -> float:
        return self.ambiguous_trades / self.trades if self.trades else 0.0

    @property
    def expectancy_confident(self) -> bool:
        """Is the expectancy more than two standard errors from zero?

        Not a formal test - it ignores the multiple comparisons made while
        arriving here, so it is a floor rather than a bar. Failing it means the
        result is indistinguishable from luck.
        """
        return self.trades >= 30 and abs(self.expectancy_r) > 2 * self.expectancy_stderr

    def __str__(self) -> str:
        if not self.trades:
            return "no trades"

        verdict = "distinguishable from luck" if self.expectancy_confident else (
            "NOT distinguishable from luck at this sample size"
        )
        lines = [
            f"  trades            {self.trades:>8,}",
            f"  expectancy        {self.expectancy_r:>+8.3f} R  (+/- {self.expectancy_stderr:.3f} se)",
            f"                    {verdict}",
            f"  total             {self.total_r:>+8.2f} R   {self.total_pnl:>+10.2f}",
            "",
            f"  win rate          {self.win_rate * 100:>7.1f}%   ({self.wins} / {self.trades})",
            f"  average win       {self.average_win_r:>+8.3f} R",
            f"  average loss      {self.average_loss_r:>+8.3f} R",
            f"  payoff ratio      {self.payoff_ratio:>8.2f}",
            f"  profit factor     {self.profit_factor:>8.2f}",
            "",
            f"  max drawdown      {self.max_drawdown_r:>8.2f} R   ({self.max_drawdown_pct:.1f}% of peak)",
            f"  longest losing    {self.longest_losing_streak:>8,} trades in a row",
            f"  Sharpe (per trade){self.sharpe:>8.2f}",
        ]
        if self.ambiguous_trades:
            lines += [
                "",
                f"  ambiguous bars    {self.ambiguous_trades:>8,} "
                f"({self.ambiguous_share * 100:.1f}% of trades)",
                "                    resolved as losses; a high share means the result",
                "                    depends on an assumption the data cannot settle",
            ]
        if self.outcomes:
            lines += ["", "  outcomes:"]
            for name, count in sorted(self.outcomes.items(), key=lambda kv: -kv[1]):
                lines.append(f"    {name:<22} {count:>6,}")
        return "\n".join(lines)


def summarise(results, starting_equity: float = 0.0) -> Performance:
    """Turn a list of TradeResult into a Performance summary.

    Trades that never triggered are excluded from the statistics but counted in
    the outcome breakdown - a strategy that rarely fires is a different problem
    from one that fires and loses, and conflating them hides both.
    """
    performance = Performance()
    performance.outcomes = {}

    taken = []
    for result in results:
        performance.outcomes[result.outcome] = performance.outcomes.get(result.outcome, 0) + 1
        if result.entered:
            taken.append(result)

    if not taken:
        return performance

    r_values = np.array([t.r_multiple for t in taken], dtype=float)
    performance.r_series = list(r_values)
    performance.trades = len(taken)
    performance.ambiguous_trades = sum(1 for t in taken if t.ambiguous_bar)

    performance.wins = int((r_values > 1e-9).sum())
    performance.losses = int((r_values < -1e-9).sum())
    performance.scratches = performance.trades - performance.wins - performance.losses

    performance.total_r = float(r_values.sum())
    performance.total_pnl = float(sum(t.pnl for t in taken))
    performance.expectancy_r = float(r_values.mean())
    performance.expectancy_stderr = (
        float(r_values.std(ddof=1) / math.sqrt(len(r_values))) if len(r_values) > 1 else 0.0
    )

    performance.win_rate = performance.wins / performance.trades
    wins = r_values[r_values > 1e-9]
    losses = r_values[r_values < -1e-9]
    performance.average_win_r = float(wins.mean()) if wins.size else 0.0
    performance.average_loss_r = float(losses.mean()) if losses.size else 0.0
    performance.payoff_ratio = (
        abs(performance.average_win_r / performance.average_loss_r)
        if performance.average_loss_r else 0.0
    )
    gross_profit = float(wins.sum())
    gross_loss = abs(float(losses.sum()))
    performance.profit_factor = gross_profit / gross_loss if gross_loss else float("inf")

    equity = np.cumsum(r_values)
    peaks = np.maximum.accumulate(np.concatenate([[0.0], equity]))[1:]
    drawdowns = peaks - equity
    performance.max_drawdown_r = float(drawdowns.max()) if drawdowns.size else 0.0
    positive_peak = peaks[drawdowns.argmax()] if drawdowns.size else 0.0
    performance.max_drawdown_pct = (
        100.0 * performance.max_drawdown_r / positive_peak if positive_peak > 0 else 0.0
    )

    performance.longest_losing_streak = _longest_streak(r_values < -1e-9)

    spread = r_values.std(ddof=1) if len(r_values) > 1 else 0.0
    performance.sharpe = float(performance.expectancy_r / spread) if spread else 0.0
    return performance


def _longest_streak(flags) -> int:
    longest = current = 0
    for flag in flags:
        current = current + 1 if flag else 0
        longest = max(longest, current)
    return longest


def r_distribution(performance: Performance, buckets: int = 12) -> pd.Series:
    """How the outcomes are spread, for spotting a result carried by one trade."""
    if not performance.r_series:
        return pd.Series(dtype=int)
    values = pd.Series(performance.r_series)
    return values.value_counts(bins=buckets).sort_index()
