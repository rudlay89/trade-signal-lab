"""Data quality checks.

Bad data does not announce itself. A backtest run on a series with a stale-price
plateau, a decimal glitch, or bars stamped in the wrong timezone will produce
numbers that look entirely normal and mean nothing. These checks are what stand
between "the strategy has an edge" and "the data had a bug".

Everything here reports rather than repairs. Silently patching data hides the
problem, and the fix (re-download, or exclude the range) depends on what went
wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Finding:
    severity: str          # "error" | "warning" | "info"
    code: str
    message: str
    count: int = 0
    examples: tuple = ()

    def __str__(self) -> str:
        head = f"[{self.severity.upper():7}] {self.code}: {self.message}"
        if self.examples:
            shown = ", ".join(str(e) for e in self.examples[:3])
            more = f" (+{self.count - len(self.examples[:3])} more)" if self.count > 3 else ""
            head += f"\n           e.g. {shown}{more}"
        return head


@dataclass
class QualityReport:
    symbol: str
    timeframe: str
    bar_count: int
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def ok(self) -> bool:
        """True when nothing found would invalidate a backtest."""
        return not self.errors

    def __str__(self) -> str:
        head = f"{self.symbol} {self.timeframe}: {self.bar_count} bars"
        if not self.findings:
            return head + "\n  no issues found"
        return head + "\n" + "\n".join("  " + str(f) for f in self.findings)


def check_bars(
    bars: pd.DataFrame,
    symbol: str,
    timeframe: str,
    *,
    max_gap_bars: int = 3,
    spike_sigma: float = 12.0,
    max_flat_bars: int = 30,
) -> QualityReport:
    """Run every check over a bar series."""
    report = QualityReport(symbol=symbol, timeframe=timeframe, bar_count=len(bars))

    if bars.empty:
        report.findings.append(Finding("error", "empty", "no bars in the requested range"))
        return report

    _check_index(bars, report)
    _check_ohlc_consistency(bars, report)
    _check_non_positive(bars, report)
    _check_spikes(bars, report, spike_sigma)
    _check_flat(bars, report, max_flat_bars)
    _check_gaps(bars, report, timeframe, max_gap_bars)
    _check_weekend(bars, report)
    _check_spread(bars, report)
    return report


def _check_index(bars: pd.DataFrame, report: QualityReport) -> None:
    if bars.index.tz is None:
        report.findings.append(
            Finding("error", "naive_index",
                    "bar index has no timezone; session logic would silently use the wrong hours")
        )
    if not bars.index.is_monotonic_increasing:
        report.findings.append(
            Finding("error", "unsorted", "bars are not in chronological order")
        )
    dupes = int(bars.index.duplicated().sum())
    if dupes:
        report.findings.append(
            Finding("error", "duplicate_timestamps", "repeated bar timestamps", dupes,
                    tuple(bars.index[bars.index.duplicated()][:3]))
        )


def _check_ohlc_consistency(bars: pd.DataFrame, report: QualityReport) -> None:
    """high must be the highest and low the lowest. A violation means the bars
    were built wrongly, and every indicator downstream is affected."""
    bad = bars[
        (bars["high"] < bars["low"])
        | (bars["high"] < bars[["open", "close"]].max(axis=1))
        | (bars["low"] > bars[["open", "close"]].min(axis=1))
    ]
    if len(bad):
        report.findings.append(
            Finding("error", "ohlc_inconsistent",
                    "high/low do not bracket open/close", len(bad), tuple(bad.index[:3]))
        )


def _check_non_positive(bars: pd.DataFrame, report: QualityReport) -> None:
    cols = ["open", "high", "low", "close"]
    bad = bars[(bars[cols] <= 0).any(axis=1) | bars[cols].isna().any(axis=1)]
    if len(bad):
        report.findings.append(
            Finding("error", "bad_prices", "zero, negative or missing prices",
                    len(bad), tuple(bad.index[:3]))
        )


def _check_spikes(bars: pd.DataFrame, report: QualityReport, sigma: float) -> None:
    """Single-bar moves far outside the recent distribution.

    Usually a bad tick rather than a real move. Reported as a warning, not an
    error: genuine gaps (a Monday open after weekend news) look identical, and
    only a human can tell them apart.

    The scale estimate is the median absolute deviation, NOT the standard
    deviation. A standard deviation is itself inflated by the very outliers it
    is meant to detect - one big spike raises the threshold enough to hide
    itself. MAD is barely moved by a handful of extreme values, so the spike
    still stands out against it.
    """
    returns = np.log(bars["close"]).diff()
    if returns.notna().sum() < 30:
        return

    centre = returns.median()
    # 1.4826 rescales MAD to be comparable with a standard deviation on normal data.
    scale = 1.4826 * (returns - centre).abs().median()
    if not np.isfinite(scale) or scale == 0:
        return
    outliers = bars[(returns - centre).abs() > sigma * scale]
    if len(outliers):
        report.findings.append(
            Finding("warning", "price_spike",
                    f"single-bar moves beyond {sigma:g} standard deviations - check for bad ticks",
                    len(outliers), tuple(outliers.index[:3]))
        )


def _check_flat(bars: pd.DataFrame, report: QualityReport, max_flat: int) -> None:
    """Long runs of an unchanged close usually mean a stalled feed, not a calm
    market. A strategy will happily 'trade' a frozen price."""
    unchanged = bars["close"].diff() == 0
    runs = unchanged.ne(unchanged.shift()).cumsum()
    lengths = unchanged.groupby(runs).transform("sum").where(unchanged, 0)
    worst = int(lengths.max()) if len(lengths) else 0
    if worst > max_flat:
        starts = bars.index[(lengths > max_flat) & (~unchanged.shift(fill_value=False))]
        report.findings.append(
            Finding("warning", "flat_price",
                    f"close unchanged for up to {worst} consecutive bars - possible stalled feed",
                    worst, tuple(starts[:3]))
        )


def _check_gaps(bars: pd.DataFrame, report: QualityReport, timeframe: str, max_gap_bars: int) -> None:
    """Missing bars inside what should be a continuous trading week.

    Weekend gaps are expected and excluded. What matters is a hole on a Tuesday
    afternoon, which means the download was incomplete.
    """
    try:
        step = pd.Timedelta(timeframe)
    except ValueError:
        return

    deltas = bars.index.to_series().diff()
    suspicious = deltas > step * max_gap_bars

    # Exclude gaps that span a weekend: Friday evening to Sunday evening UTC.
    starts = bars.index.to_series().shift()
    is_weekend_gap = (starts.dt.dayofweek == 4) | (bars.index.to_series().dt.dayofweek == 6)
    holes = bars.index[suspicious & ~is_weekend_gap]

    if len(holes):
        report.findings.append(
            Finding("warning", "missing_bars",
                    f"gaps longer than {max_gap_bars} bars inside the trading week",
                    len(holes), tuple(holes[:3]))
        )


def _check_weekend(bars: pd.DataFrame, report: QualityReport) -> None:
    """Ticks on a Saturday almost always mean a timezone error - which would put
    every session boundary in the wrong place."""
    saturday = bars.index[bars.index.dayofweek == 5]
    if len(saturday):
        report.findings.append(
            Finding("error", "weekend_data",
                    "bars timestamped on a Saturday; the timezone is probably wrong",
                    len(saturday), tuple(saturday[:3]))
        )


def _check_spread(bars: pd.DataFrame, report: QualityReport) -> None:
    if "spread_mean" not in bars:
        return
    negative = bars[bars["spread_mean"] < 0]
    if len(negative):
        report.findings.append(
            Finding("error", "negative_spread", "mean spread is negative; bid and ask are swapped",
                    len(negative), tuple(negative.index[:3]))
        )
    if "spread_max" in bars and len(bars) > 30:
        typical = bars["spread_mean"].median()
        if typical > 0:
            extreme = bars[bars["spread_max"] > typical * 50]
            if len(extreme):
                report.findings.append(
                    Finding("info", "spread_blowout",
                            "spread exceeded 50x its median - normal around news, but these "
                            "bars will be expensive to trade",
                            len(extreme), tuple(extreme.index[:3]))
                )
