"""Turning ticks into bars.

Two decisions here matter more than they look, because both are ways a backtest
can quietly cheat:

BAR LABELLING. A bar is stamped with the START of its interval and contains
[t, t + freq). So the 09:15 bar on a 15-minute series covers 09:15:00 to
09:29:59.999. A strategy may only act on a bar once that interval has CLOSED -
at 09:30. Labelling bars by their end, or acting on the open of the bar you are
still inside, is look-ahead bias, and it makes almost any strategy look
profitable.

EMPTY INTERVALS ARE DROPPED, NOT FILLED. FX has real gaps: weekends, the daily
rollover, holidays. Forward-filling them invents prices that never traded and
lets a strategy "trade" through a closed market. Gaps are left as gaps and
reported by the quality checks instead.
"""

from __future__ import annotations

import pandas as pd

BAR_COLUMNS = (
    "open", "high", "low", "close",
    "spread_mean", "spread_max",
    "tick_count", "volume",
)


def ticks_to_bars(ticks: pd.DataFrame, freq: str) -> pd.DataFrame:
    """Aggregate ticks into OHLC bars at `freq` (e.g. "1min", "15min").

    OHLC is computed on the MID price. The spread is carried alongside rather
    than baked in, so the cost model can apply it explicitly at fill time: a buy
    pays close + spread/2, a sell receives close - spread/2. Baking a fixed
    spread into stored prices makes it impossible to re-test a different cost
    assumption later.
    """
    required = {"timestamp", "bid", "ask"}
    missing = required - set(ticks.columns)
    if missing:
        raise ValueError(f"ticks is missing column(s): {', '.join(sorted(missing))}")

    if ticks.empty:
        return empty_bars()

    frame = ticks.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["mid"] = (frame["bid"] + frame["ask"]) / 2.0
    frame["spread"] = frame["ask"] - frame["bid"]
    if "bid_volume" in frame and "ask_volume" in frame:
        frame["vol"] = frame["bid_volume"].fillna(0) + frame["ask_volume"].fillna(0)
    else:
        frame["vol"] = 0.0

    frame = frame.set_index("timestamp").sort_index()

    # label="left", closed="left": the bar is named by the instant it opens and
    # covers up to (not including) the next one. See the module docstring.
    grouped = frame.resample(freq, label="left", closed="left")

    bars = pd.DataFrame({
        "open": grouped["mid"].first(),
        "high": grouped["mid"].max(),
        "low": grouped["mid"].min(),
        "close": grouped["mid"].last(),
        "spread_mean": grouped["spread"].mean(),
        "spread_max": grouped["spread"].max(),
        "tick_count": grouped["mid"].count(),
        "volume": grouped["vol"].sum(),
    })

    # Drop intervals with no ticks rather than inventing prices for them.
    bars = bars[bars["tick_count"] > 0]
    bars["tick_count"] = bars["tick_count"].astype("int64")
    bars.index.name = "timestamp"
    return bars


def empty_bars() -> pd.DataFrame:
    frame = pd.DataFrame(
        {c: pd.Series([], dtype="int64" if c == "tick_count" else "float64") for c in BAR_COLUMNS}
    )
    frame.index = pd.DatetimeIndex([], tz="UTC", name="timestamp")
    return frame


def resample_bars(bars: pd.DataFrame, freq: str) -> pd.DataFrame:
    """Aggregate existing bars to a coarser timeframe (1min -> 15min -> 1h).

    Only ever coarser. Going the other way would require inventing detail that
    was thrown away, so callers should build fine bars once and roll up.
    """
    if bars.empty:
        return empty_bars()

    # Weight the spread by tick count, so a two-tick bar does not count as much
    # as a five-hundred-tick one. Done by summing a product column rather than
    # with .apply, which is both slower and awkward across pandas versions.
    working = bars.copy()
    working["_spread_weighted"] = working["spread_mean"] * working["tick_count"]

    grouped = working.resample(freq, label="left", closed="left")
    tick_count = grouped["tick_count"].sum()

    out = pd.DataFrame({
        "open": grouped["open"].first(),
        "high": grouped["high"].max(),
        "low": grouped["low"].min(),
        "close": grouped["close"].last(),
        "spread_mean": grouped["_spread_weighted"].sum() / tick_count.replace(0, pd.NA),
        "spread_max": grouped["spread_max"].max(),
        "tick_count": tick_count,
        "volume": grouped["volume"].sum(),
    })
    out = out[out["tick_count"] > 0]
    out["tick_count"] = out["tick_count"].astype("int64")
    out.index.name = "timestamp"
    return out

