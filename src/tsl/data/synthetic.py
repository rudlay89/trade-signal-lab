"""Synthetic tick and bar generation, for tests and for exercising the pipeline
without a network connection.

This is deliberately NOT a market simulator. It produces data with the right
shape, dtypes and edge cases so the plumbing can be tested; it makes no claim to
realistic price dynamics, and nothing that touches it should ever be presented as
a backtest result.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd


def synthetic_ticks(
    start: datetime,
    hours: float = 1.0,
    ticks_per_hour: int = 3600,
    start_price: float = 1.10000,
    spread: float = 0.00012,
    volatility: float = 0.00002,
    seed: int = 0,
) -> pd.DataFrame:
    """A random walk sampled at regular intervals, as bid/ask ticks."""
    if start.tzinfo is None:
        raise ValueError("start must be timezone-aware")

    rng = np.random.default_rng(seed)
    n = max(int(hours * ticks_per_hour), 1)

    steps = rng.normal(0.0, volatility, size=n)
    mid = start_price + np.cumsum(steps)

    total_ms = int(hours * 3600 * 1000)
    offsets = np.linspace(0, total_ms, n, endpoint=False).astype(np.int64)

    return pd.DataFrame({
        "timestamp": pd.to_datetime(start.astimezone(timezone.utc)) + pd.to_timedelta(offsets, unit="ms"),
        "bid": mid - spread / 2.0,
        "ask": mid + spread / 2.0,
        "bid_volume": rng.uniform(0.1, 5.0, size=n),
        "ask_volume": rng.uniform(0.1, 5.0, size=n),
    })


def synthetic_bi5(ticks: pd.DataFrame, hour_start: datetime, point_scale: float) -> bytes:
    """Encode ticks into the Dukascopy .bi5 wire format.

    Used to test the decoder round-trip. If the real format ever differs from
    what dukascopy.py assumes, this will not catch it - only real data can. It
    does catch the decoder contradicting its own documented layout.
    """
    import lzma
    import struct

    hour_start = hour_start.astimezone(timezone.utc)
    out = bytearray()
    for row in ticks.itertuples(index=False):
        ms = int((pd.Timestamp(row.timestamp).to_pydatetime() - hour_start) / timedelta(milliseconds=1))
        out += struct.pack(
            ">IIIff",
            ms,
            int(round(row.ask * point_scale)),
            int(round(row.bid * point_scale)),
            float(row.ask_volume),
            float(row.bid_volume),
        )
    return lzma.compress(bytes(out), format=lzma.FORMAT_ALONE)


def synthetic_candle_file(
    bars,
    day,
    point_scale: float,
    *,
    field_order=("open", "close", "low", "high"),
    as_floats: bool = False,
) -> bytes:
    """Encode bars into the Dukascopy daily candle wire format.

    Prices are int32 scaled by `point_scale`, as the real files use.

    `field_order` lets tests write the fields in the WRONG order and prove the
    decoder eliminates it. `as_floats` reproduces the original mistaken
    assumption - a same-width record of float32 prices - so the regression is
    covered rather than merely remembered.
    """
    import lzma
    import struct

    day = day.astimezone(timezone.utc)
    out = bytearray()
    for ts, row in bars.iterrows():
        offset = int((pd.Timestamp(ts).to_pydatetime() - day).total_seconds())
        prices = [float(row[name]) for name in field_order]
        if as_floats:
            out += struct.pack(">i5f", offset, *prices, float(row.get("volume", 0.0)))
        else:
            out += struct.pack(
                ">5if",
                offset,
                *[int(round(p * point_scale)) for p in prices],
                float(row.get("volume", 0.0)),
            )
    return lzma.compress(bytes(out), format=lzma.FORMAT_ALONE)
