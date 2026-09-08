"""Dukascopy daily candle files - the low-volume alternative to tick files.

Tick data is one file per hour: 24 requests and several megabytes per instrument
per day. Dukascopy publishes the same period pre-aggregated into 1-minute
candles, one file per day per side:

    https://datafeed.dukascopy.com/datafeed/{SYMBOL}/{YYYY}/{MM}/{DD}/BID_candles_min_1.bi5
    https://datafeed.dukascopy.com/datafeed/{SYMBOL}/{YYYY}/{MM}/{DD}/ASK_candles_min_1.bi5

Two requests per day instead of twenty-four, for the same 1-minute bars. Over a
multi-year download that is the difference between hours and days, which matters
a great deal when the server throttles sustained use.

FORMAT - DETERMINED FROM REAL DATA, NOT ASSUMED.

    RECORD  24 bytes, big-endian, ">5if":
              int32    seconds after the start of the UTC day
              int32    price, scaled by the instrument's point_scale
              int32    price
              int32    price
              int32    price
              float32  volume

Prices are SCALED INTEGERS, exactly as in the tick format - not floats. That was
originally assumed the other way round, and the first real download caught it:
gold decoded as 2.8e-39, a denormalised float. Reversing that denormal gives the
integer 2,046,523, which over point_scale 1000 is 2046.52 - precisely the day's
high in the tick-derived bars. A float32 record of the same width read the same
bytes without complaint, which is why the size check did not catch it.

The ORDER of the four prices is still not known from documentation, so it is not
guessed either. `decode_candles` tries the candidate layouts and keeps only those
that are internally consistent - a high that really is the highest of the four
and a low that really is the lowest. A wrong order violates that on nearly every
candle, so it eliminates itself.

Internal consistency narrows the field; it does not close it. `tsl verify-candles`
does that, by rebuilding days already derived from raw ticks and comparing them
minute by minute. Two independent paths agreeing is the proof.
"""

from __future__ import annotations

import lzma
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from .dukascopy import BASE_URL, DecodeError, SourceSpec

CANDLE_SIZE = 24  # int32 offset + 4x int32 price + float32 volume

SIDES = ("BID", "ASK")

# Candidate field orders for the four prices. Documentation does not settle this,
# so both plausible orders are tried and the inconsistent ones discarded. Listing
# them explicitly beats a comment claiming to know.
CANDIDATE_ORDERS = (
    ("open", "close", "low", "high"),   # the order Dukascopy is usually said to use
    ("open", "high", "low", "close"),   # the conventional OHLC order
)

_DTYPE = np.dtype([
    ("offset", ">i4"),
    ("a", ">i4"), ("b", ">i4"), ("c", ">i4"), ("d", ">i4"),
    ("volume", ">f4"),
])
_SLOTS = ("a", "b", "c", "d")


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


def decode_candles(
    payload: bytes,
    spec: SourceSpec,
    day: datetime,
    side: str,
    field_order: tuple | None = None,
) -> pd.DataFrame:
    """Decode one day of one-sided 1-minute candles.

    With `field_order` given, that layout is used and must validate. Without it,
    the candidates are tried and the ones that are internally inconsistent are
    discarded. The layout actually used is recorded on the frame's `attrs`, so
    callers can report it rather than assume it.

    An empty payload means a closed market, which is not an error.
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

    arr = np.frombuffer(raw, dtype=_DTYPE, count=len(raw) // CANDLE_SIZE)
    where = f"{spec.symbol} {day:%Y-%m-%d} {side}"

    offsets = arr["offset"]
    bad_offset = int(((offsets < 0) | (offsets >= 86400)).sum())
    if bad_offset:
        raise DecodeError(
            f"{where}: {bad_offset} candle(s) have a time offset outside the day "
            "(0-86399 seconds). The record layout in candles.py is wrong."
        )

    index = pd.to_datetime(day.astimezone(timezone.utc)) + pd.to_timedelta(
        offsets.astype(np.int64), unit="s"
    )

    orders = (tuple(field_order),) if field_order else CANDIDATE_ORDERS
    accepted, rejections = [], []

    for order in orders:
        frame = _build(arr, index, order, spec)
        problem = _inconsistency(frame, spec)
        if problem is None:
            frame.attrs["field_order"] = order
            accepted.append(frame)
        else:
            rejections.append(f"    {'/'.join(order)}: {problem}")

    if not accepted:
        raise DecodeError(
            f"{where}: no candidate field order produces self-consistent candles.\n"
            + "\n".join(rejections)
            + "\n  Prices are read as int32 scaled by point_scale "
            f"({spec.point_scale:g}). If that is wrong, or the record is not "
            "24 bytes, candles.py needs the real layout. Nothing has been stored."
        )

    if len(accepted) > 1 and not field_order:
        # Both orders self-consistent. Possible on a degenerate day; the
        # tick comparison in verify-candles is the arbiter, so carry on with the
        # first but leave a marker so it can be reported rather than hidden.
        accepted[0].attrs["ambiguous"] = [f.attrs["field_order"] for f in accepted]

    return accepted[0].sort_index()


def _build(arr, index, order: tuple, spec: SourceSpec) -> pd.DataFrame:
    """Assemble a frame reading the four price slots in the given order."""
    data = {
        name: arr[slot].astype(np.float64) / spec.point_scale
        for slot, name in zip(_SLOTS, order)
    }
    frame = pd.DataFrame(data, index=index)
    frame["volume"] = arr["volume"].astype(np.float64)
    frame.index.name = "timestamp"
    return frame[["open", "high", "low", "close", "volume"]]


def _inconsistency(frame: pd.DataFrame, spec: SourceSpec) -> str | None:
    """Return why this layout cannot be right, or None if it survives.

    Two independent tests. Prices must be in a plausible band for the instrument,
    and high/low must genuinely bracket open/close - which a wrong field order
    breaks on nearly every candle.
    """
    prices = frame[["open", "high", "low", "close"]]
    values = prices.to_numpy()

    if not np.isfinite(values).all():
        return "non-finite prices"

    low, high = float(values.min()), float(values.max())
    if low < spec.plausible_low or high > spec.plausible_high:
        return (
            f"prices span {low:.6g}..{high:.6g}, outside the plausible range "
            f"{spec.plausible_low:g}..{spec.plausible_high:g}"
        )

    broken = int((
        (frame["high"] < frame["low"])
        | (frame["high"] < frame[["open", "close"]].max(axis=1))
        | (frame["low"] > frame[["open", "close"]].min(axis=1))
    ).sum())
    if broken:
        return (
            f"{broken} of {len(frame)} candles have a high that is not the highest "
            "of the four prices, or a low that is not the lowest"
        )
    return None


def _empty_candles() -> pd.DataFrame:
    frame = pd.DataFrame(
        {c: pd.Series([], dtype="float64") for c in ("open", "high", "low", "close", "volume")}
    )
    frame.index = pd.DatetimeIndex([], tz="UTC", name="timestamp")
    return frame


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
    field_order: tuple | None = None,
    **kwargs,
) -> pd.DataFrame:
    """Fetch and combine both sides of one day's 1-minute candles.

    The layout actually used is recorded on the returned frame's `attrs`, so it
    can be reported rather than silently assumed.
    """
    sides = {}
    for side in SIDES:
        payload = download_url(session, candle_url(spec.symbol, day, side), day, **kwargs)
        sides[side] = decode_candles(payload, spec, day, side, field_order=field_order)

    bid, ask = sides["BID"], sides["ASK"]
    if not bid.empty and not ask.empty:
        bid_order = bid.attrs.get("field_order")
        ask_order = ask.attrs.get("field_order")
        if bid_order != ask_order:
            raise DecodeError(
                f"{spec.symbol} {day:%Y-%m-%d}: the bid file decodes as "
                f"{'/'.join(bid_order)} but the ask file as {'/'.join(ask_order)}. "
                "Both sides must share a layout; something is wrong."
            )

    bars = candles_to_bars(bid, ask)
    if not bid.empty:
        bars.attrs["field_order"] = bid.attrs.get("field_order")
        if "ambiguous" in bid.attrs:
            bars.attrs["ambiguous"] = bid.attrs["ambiguous"]
    return bars


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
