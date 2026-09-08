"""Dukascopy daily candle files - the low-volume alternative to tick files.

Tick data is one file per hour: 24 requests and several megabytes per instrument
per day. Dukascopy publishes the same period pre-aggregated into 1-minute
candles, one file per day per side:

    https://datafeed.dukascopy.com/datafeed/{SYMBOL}/{YYYY}/{MM}/{DD}/BID_candles_min_1.bi5
    https://datafeed.dukascopy.com/datafeed/{SYMBOL}/{YYYY}/{MM}/{DD}/ASK_candles_min_1.bi5

Two requests per day instead of twenty-four, for the same 1-minute bars. Over a
multi-year download that is the difference between hours and days, which matters
a great deal when the server throttles sustained use.

FORMAT - ASSUMED, AND VERIFIED BY COMPARISON RATHER THAN BY FAITH.

    RECORD  24 bytes, big-endian, ">i5f":
              int32    seconds after the start of the UTC day
              float32  open
              float32  close     <- NOTE the order: open, CLOSE, low, high
              float32  low
              float32  high
              float32  volume

    Prices are plain floats here, NOT integers needing point_scale, which is the
    main difference from the tick format.

The open/close/low/high ordering is the part most likely to be wrong, and it is
an unusual order that is easy to assume away. Two defences:

  1. `decode_candles` enforces OHLC consistency - high must be the highest of the
     four and low the lowest. Reading the fields in the wrong order breaks that
     on nearly every candle, so a mistake fails immediately.
  2. `tsl verify-candles` downloads days that were already built from ticks and
     compares the two bar series directly. Agreement to a fraction of a spread
     proves the layout; nothing else does.

Do not trust this module for a large download until step 2 has passed.
"""

from __future__ import annotations

import lzma
import struct
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from .dukascopy import BASE_URL, DecodeError, SourceSpec

CANDLE = struct.Struct(">i5f")
CANDLE_SIZE = CANDLE.size  # 24

SIDES = ("BID", "ASK")


def candle_url(symbol: str, day: datetime, side: str) -> str:
    """URL for one day of 1-minute candles on one side of the book."""
    if side not in SIDES:
        raise ValueError(f"side must be one of {SIDES}, got {side!r}")
    if day.tzinfo is None:
        raise ValueError("`day` must be timezone-aware; naive datetimes are ambiguous")
    day = day.astimezone(timezone.utc)
    # Month is zero-indexed here too, exactly as for tick files.
    return (
        f"{BASE_URL}/{symbol}/{day.year:04d}/{day.month - 1:02d}/{day.day:02d}/"
        f"{side}_candles_min_1.bi5"
    )


def decode_candles(payload: bytes, spec: SourceSpec, day: datetime, side: str) -> pd.DataFrame:
    """Decode one day of one-sided 1-minute candles.

    Returns a frame indexed by timestamp with open/high/low/close/volume. An
    empty payload means a closed market, which is not an error.
    """
    if day.tzinfo is None:
        raise ValueError("`day` must be timezone-aware UTC")

    if not payload:
        return _empty_candles()

    try:
        raw = lzma.decompress(payload, format=lzma.FORMAT_AUTO)
    except lzma.LZMAError as exc:
        raise DecodeError(
            f"{spec.symbol} {day:%Y-%m-%d} {side}: not valid LZMA data "
            f"({len(payload)} bytes)."
        ) from exc

    if not raw:
        return _empty_candles()

    if len(raw) % CANDLE_SIZE:
        raise DecodeError(
            f"{spec.symbol} {day:%Y-%m-%d} {side}: {len(raw)} bytes is not a whole "
            f"number of {CANDLE_SIZE}-byte candles. Either the file is truncated or "
            "the record layout assumed in candles.py is wrong."
        )

    arr = np.frombuffer(raw, dtype=np.dtype([
        ("offset", ">i4"),
        ("open", ">f4"), ("close", ">f4"), ("low", ">f4"), ("high", ">f4"),
        ("volume", ">f4"),
    ]), count=len(raw) // CANDLE_SIZE)

    frame = pd.DataFrame({
        "open": arr["open"].astype(np.float64),
        "high": arr["high"].astype(np.float64),
        "low": arr["low"].astype(np.float64),
        "close": arr["close"].astype(np.float64),
        "volume": arr["volume"].astype(np.float64),
    })
    frame.index = pd.to_datetime(day.astimezone(timezone.utc)) + pd.to_timedelta(
        arr["offset"].astype(np.int64), unit="s"
    )
    frame.index.name = "timestamp"

    _validate_candles(frame, arr["offset"], spec, day, side)
    return frame.sort_index()


def _empty_candles() -> pd.DataFrame:
    frame = pd.DataFrame(
        {c: pd.Series([], dtype="float64") for c in ("open", "high", "low", "close", "volume")}
    )
    frame.index = pd.DatetimeIndex([], tz="UTC", name="timestamp")
    return frame


def _validate_candles(frame, offsets, spec: SourceSpec, day: datetime, side: str) -> None:
    """Catch a wrong record layout on the first file, not after a year of downloading."""
    where = f"{spec.symbol} {day:%Y-%m-%d} {side}"

    bad_offset = int(((offsets < 0) | (offsets >= 86400)).sum())
    if bad_offset:
        raise DecodeError(
            f"{where}: {bad_offset} candle(s) have a time offset outside the day "
            "(0-86399 seconds). The record layout in candles.py is wrong."
        )

    prices = frame[["open", "high", "low", "close"]]
    if not np.isfinite(prices.to_numpy()).all():
        raise DecodeError(f"{where}: non-finite prices decoded; the record layout is wrong.")

    low, high = float(prices.min().min()), float(prices.max().max())
    if low < spec.plausible_low or high > spec.plausible_high:
        raise DecodeError(
            f"{where}: prices span {low:.6g}..{high:.6g}, outside the plausible range "
            f"{spec.plausible_low:g}..{spec.plausible_high:g}. Candle prices are plain "
            "floats and need no point_scale, so this points at the record layout."
        )

    # THE ORDERING CHECK. If open/close/low/high were read in the wrong order, the
    # nominal high stops being the highest of the four on almost every candle.
    inconsistent = int((
        (frame["high"] < frame["low"])
        | (frame["high"] < frame[["open", "close"]].max(axis=1))
        | (frame["low"] > frame[["open", "close"]].min(axis=1))
    ).sum())
    if inconsistent:
        raise DecodeError(
            f"{where}: {inconsistent} of {len(frame)} candles have a high that is not "
            "the highest of the four prices, or a low that is not the lowest.\n"
            "That is what a wrong field order looks like. candles.py assumes the "
            "order open, CLOSE, low, high - if Dukascopy uses open, high, low, close "
            "instead, that is the fix. Nothing has been stored."
        )


def candles_to_bars(bid: pd.DataFrame, ask: pd.DataFrame) -> pd.DataFrame:
    """Combine one-sided candles into the mid-price bars the rest of the code uses.

    Produces the same columns as `bars.ticks_to_bars`, so downstream code cannot
    tell which source a series came from. The spread is carried separately rather
    than baked into prices, exactly as with ticks.
    """
    from .bars import empty_bars

    if bid.empty or ask.empty:
        return empty_bars()

    joined = bid.join(ask, how="inner", lsuffix="_bid", rsuffix="_ask")
    if joined.empty:
        return empty_bars()

    out = pd.DataFrame(index=joined.index)
    for column in ("open", "high", "low", "close"):
        out[column] = (joined[f"{column}_bid"] + joined[f"{column}_ask"]) / 2.0

    spread = joined["close_ask"] - joined["close_bid"]
    out["spread_mean"] = spread
    out["spread_max"] = joined["high_ask"] - joined["low_bid"]
    # Candle files carry volume but not a tick count. Zero is honest: this series
    # genuinely does not know, and inventing a number would be worse.
    out["tick_count"] = 1
    out["volume"] = joined["volume_bid"] + joined["volume_ask"]

    out["tick_count"] = out["tick_count"].astype("int64")
    out.index.name = "timestamp"
    return out.sort_index()


def days_between(start: datetime, end: datetime):
    """Yield each UTC day start in [start, end), oldest first."""
    cursor = start.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    end = end.astimezone(timezone.utc)
    while cursor < end:
        yield cursor
        cursor += timedelta(days=1)


# --- downloading and verification --------------------------------------------

from typing import Callable  # noqa: E402

import requests  # noqa: E402

from .dukascopy import download_url  # noqa: E402


def download_day_candles(
    session: requests.Session,
    spec: SourceSpec,
    day: datetime,
    **kwargs,
) -> pd.DataFrame:
    """Fetch and combine both sides of one day's 1-minute candles."""
    sides = {}
    for side in SIDES:
        payload = download_url(session, candle_url(spec.symbol, day, side), day, **kwargs)
        sides[side] = decode_candles(payload, spec, day, side)
    return candles_to_bars(sides["BID"], sides["ASK"])


def compare_bar_series(from_ticks: pd.DataFrame, from_candles: pd.DataFrame) -> dict:
    """Compare two 1-minute bar series built from different sources.

    This is the only real proof that the candle record layout is right. A wrong
    field order might survive the consistency checks on some pathological day;
    it cannot survive matching, minute for minute, a series independently built
    from raw ticks.
    """
    shared = from_ticks.index.intersection(from_candles.index)
    result = {
        "tick_bars": len(from_ticks),
        "candle_bars": len(from_candles),
        "overlapping": len(shared),
    }
    if len(shared) == 0:
        result["verdict"] = "no overlap - nothing to compare"
        return result

    a, b = from_ticks.loc[shared], from_candles.loc[shared]
    spread = float(a["spread_mean"].median()) if "spread_mean" in a else 0.0

    for column in ("open", "high", "low", "close"):
        diff = (a[column] - b[column]).abs()
        result[f"{column}_max_diff"] = float(diff.max())
        result[f"{column}_median_diff"] = float(diff.median())

    worst = max(result[f"{c}_max_diff"] for c in ("open", "high", "low", "close"))
    result["worst_diff"] = worst
    result["typical_spread"] = spread

    # Tolerance of one spread. The two series are built differently - ticks give
    # the extremes of the mid price, candles average the extremes of each side -
    # so exact equality is not expected. Agreement within a spread means the same
    # market; a wrong field order would be out by many multiples of it.
    result["passed"] = worst <= max(spread, 1e-9) * 1.5
    result["verdict"] = (
        "MATCH - the candle format is correct"
        if result["passed"]
        else "MISMATCH - do not use candle downloads until this is resolved"
    )
    return result
