"""Dukascopy tick data: URL construction, decoding, and download.

Dukascopy publishes free historical tick data as one LZMA-compressed file per
instrument per UTC hour. Each file holds fixed-width binary records.

FORMAT NOTES - PARTLY UNVERIFIED. Read this before trusting the output.

    URL    https://datafeed.dukascopy.com/datafeed/{SYMBOL}/{YYYY}/{MM}/{DD}/{HH}h_ticks.bi5
           MM is ZERO-INDEXED: January is 00, December is 11. Getting this wrong
           silently fetches the wrong month's data, which is far worse than an
           error, so `hour_url` is covered by tests.

    BODY   LZMA-compressed. Empty (zero-length) for hours with no ticks -
           weekends, holidays, and the daily rollover gap. That is normal, not
           an error.

    RECORD 20 bytes, big-endian, ">IIIff":
             uint32   milliseconds after the start of the hour
             uint32   ask, as an integer scaled by point_scale
             uint32   bid, as an integer scaled by point_scale
             float32  ask volume
             float32  bid volume

The layout above is high confidence - it is consistent across several independent
open-source implementations. The per-instrument `point_scale` is LESS certain,
and could not be checked against real data while writing this (the datafeed host
is blocked by network policy in the authoring environment).

That uncertainty is handled rather than hoped away: every decoded price is checked
against the instrument's configured `plausible_price` range, and a violation
raises rather than storing the data. A wrong scale is wrong by a factor of ten or
more, so the first download of any instrument will either look right or fail
loudly. It cannot quietly poison the backtest.
"""

from __future__ import annotations

import lzma
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

BASE_URL = "https://datafeed.dukascopy.com/datafeed"
RECORD = struct.Struct(">IIIff")
RECORD_SIZE = RECORD.size  # 20

# Columns produced by `decode_hour`, in order.
TICK_COLUMNS = ("timestamp", "bid", "ask", "bid_volume", "ask_volume")


class DecodeError(ValueError):
    """A tick file could not be decoded, or decoded to implausible values."""


@dataclass(frozen=True)
class SourceSpec:
    """How to fetch and decode one instrument from Dukascopy."""

    symbol: str
    point_scale: float
    plausible_low: float
    plausible_high: float

    @classmethod
    def from_config(cls, body: dict) -> "SourceSpec":
        low, high = body["plausible_price"]
        if not 0 < float(low) < float(high):
            raise ValueError(f"plausible_price must be an increasing positive pair, got {body['plausible_price']}")
        if float(body["point_scale"]) <= 0:
            raise ValueError("point_scale must be positive")
        return cls(
            symbol=body["symbol"],
            point_scale=float(body["point_scale"]),
            plausible_low=float(low),
            plausible_high=float(high),
        )


def hour_url(symbol: str, when: datetime) -> str:
    """Build the .bi5 URL for one UTC hour.

    `when` must be timezone-aware UTC. The month is zero-indexed in the path,
    which is the single most common mistake with this feed.
    """
    if when.tzinfo is None:
        raise ValueError("`when` must be timezone-aware; naive datetimes are ambiguous")
    when = when.astimezone(timezone.utc)
    return (
        f"{BASE_URL}/{symbol}/{when.year:04d}/{when.month - 1:02d}/"
        f"{when.day:02d}/{when.hour:02d}h_ticks.bi5"
    )


def decode_hour(payload: bytes, spec: SourceSpec, hour_start: datetime) -> pd.DataFrame:
    """Decode one hour's .bi5 payload into a tick DataFrame.

    An empty payload yields an empty frame - that is how Dukascopy represents an
    hour with no trading, and it is not an error.
    """
    if hour_start.tzinfo is None:
        raise ValueError("`hour_start` must be timezone-aware UTC")

    if not payload:
        return _empty_ticks()

    try:
        raw = lzma.decompress(payload, format=lzma.FORMAT_AUTO)
    except lzma.LZMAError as exc:
        raise DecodeError(
            f"{spec.symbol} {hour_start:%Y-%m-%d %Hh}: not valid LZMA data "
            f"({len(payload)} bytes). A truncated download or an HTML error page "
            "saved as data would look like this."
        ) from exc

    if not raw:
        return _empty_ticks()

    if len(raw) % RECORD_SIZE:
        raise DecodeError(
            f"{spec.symbol} {hour_start:%Y-%m-%d %Hh}: {len(raw)} bytes is not a "
            f"whole number of {RECORD_SIZE}-byte records - the file is truncated "
            "or the record layout has changed."
        )

    count = len(raw) // RECORD_SIZE
    arr = np.frombuffer(raw, dtype=np.dtype([
        ("ms", ">u4"), ("ask", ">u4"), ("bid", ">u4"),
        ("ask_vol", ">f4"), ("bid_vol", ">f4"),
    ]), count=count)

    bid = arr["bid"].astype(np.float64) / spec.point_scale
    ask = arr["ask"].astype(np.float64) / spec.point_scale

    _check_plausible(bid, ask, spec, hour_start)

    frame = pd.DataFrame({
        "timestamp": pd.to_datetime(hour_start) + pd.to_timedelta(arr["ms"].astype(np.int64), unit="ms"),
        "bid": bid,
        "ask": ask,
        "bid_volume": arr["bid_vol"].astype(np.float64),
        "ask_volume": arr["ask_vol"].astype(np.float64),
    })
    return frame.sort_values("timestamp", kind="stable").reset_index(drop=True)


def _empty_ticks() -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": pd.Series([], dtype="datetime64[ms, UTC]"),
        "bid": pd.Series([], dtype="float64"),
        "ask": pd.Series([], dtype="float64"),
        "bid_volume": pd.Series([], dtype="float64"),
        "ask_volume": pd.Series([], dtype="float64"),
    })


def _check_plausible(bid, ask, spec: SourceSpec, hour_start: datetime) -> None:
    """Refuse data that cannot be right, rather than storing it.

    This is the safety net under the unverified `point_scale`. A scale wrong by a
    factor of ten puts every price far outside the configured band, so the very
    first download fails with a message that says what to change.
    """
    for name, values in (("bid", bid), ("ask", ask)):
        if values.size == 0:
            continue
        low, high = float(values.min()), float(values.max())
        if low < spec.plausible_low or high > spec.plausible_high:
            factor = high / spec.plausible_high if high > spec.plausible_high else spec.plausible_low / max(low, 1e-12)
            raise DecodeError(
                f"{spec.symbol} {hour_start:%Y-%m-%d %Hh}: decoded {name} prices span "
                f"{low:.6g}..{high:.6g}, outside the plausible range "
                f"{spec.plausible_low:g}..{spec.plausible_high:g} (off by roughly {factor:.3g}x).\n"
                f"Most likely the point_scale of {spec.point_scale:g} in config/instruments.yaml "
                f"is wrong for {spec.symbol}. Nothing has been stored."
            )

    crossed = int((bid > ask).sum())
    if crossed:
        raise DecodeError(
            f"{spec.symbol} {hour_start:%Y-%m-%d %Hh}: {crossed} tick(s) have bid above "
            "ask. The bid and ask fields are probably swapped in the record layout."
        )


def hours_between(start: datetime, end: datetime):
    """Yield each UTC hour start in [start, end), oldest first."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must be timezone-aware")
    cursor = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    end = end.astimezone(timezone.utc)
    while cursor < end:
        yield cursor
        cursor += timedelta(hours=1)


# --- downloading -------------------------------------------------------------
#
# Kept separate from decoding so the decoder can be tested without a network,
# which matters: this project was authored in an environment where Dukascopy's
# host is blocked by policy, so every decode path is covered by tests while the
# transport is deliberately thin and obvious.

import time
from typing import Callable

import requests

USER_AGENT = "trade-signal-lab/0.1 (personal research)"


class DownloadError(RuntimeError):
    """An hour could not be fetched after retrying."""


def download_hour(
    session: requests.Session,
    symbol: str,
    hour: datetime,
    *,
    attempts: int = 4,
    backoff: float = 2.0,
    timeout: float = 30.0,
) -> bytes:
    """Fetch one hour's raw payload.

    Returns empty bytes for an hour with no data. Dukascopy signals that as
    either a 404 or a zero-length 200 depending on the instrument and age, and
    both mean the same thing: the market was closed. Treating a 404 as a hard
    error would abort every download that crosses a weekend.
    """
    url = hour_url(symbol, hour)
    last: Exception | None = None

    for attempt in range(attempts):
        try:
            response = session.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
            if response.status_code == 404:
                return b""
            if response.status_code == 200:
                return response.content
            # 5xx and rate limiting are worth retrying; other 4xx are not.
            if response.status_code < 500 and response.status_code != 429:
                raise DownloadError(f"{url}: HTTP {response.status_code}")
            last = DownloadError(f"{url}: HTTP {response.status_code}")
        except requests.RequestException as exc:
            last = exc

        if attempt < attempts - 1:
            time.sleep(backoff * (2**attempt))

    raise DownloadError(f"{url}: giving up after {attempts} attempts ({last})")


def download_ticks(
    symbol: str,
    spec: SourceSpec,
    start: datetime,
    end: datetime,
    *,
    session: requests.Session | None = None,
    pause: float = 0.15,
    on_progress: Callable[[datetime, int], None] | None = None,
) -> pd.DataFrame:
    """Download and decode every hour in [start, end).

    `pause` is a courtesy delay between requests. This is a free service being
    used for personal research; hammering it is both rude and a good way to get
    blocked.
    """
    owned = session is None
    session = session or requests.Session()
    frames: list[pd.DataFrame] = []

    try:
        for hour in hours_between(start, end):
            payload = download_hour(session, spec.symbol, hour)
            frame = decode_hour(payload, spec, hour)
            if not frame.empty:
                frames.append(frame)
            if on_progress:
                on_progress(hour, len(frame))
            if pause:
                time.sleep(pause)
    finally:
        if owned:
            session.close()

    if not frames:
        return _empty_ticks()
    return pd.concat(frames, ignore_index=True).sort_values("timestamp").reset_index(drop=True)
