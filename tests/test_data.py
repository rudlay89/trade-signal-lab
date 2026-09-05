"""Data pipeline: decoding, bar construction, storage and quality checks."""

from datetime import datetime, timedelta, timezone

import lzma
import numpy as np
import pandas as pd
import pytest

from tsl.data.bars import ticks_to_bars, resample_bars
from tsl.data.dukascopy import (
    DecodeError, SourceSpec, decode_hour, hour_url, hours_between,
)
from tsl.data.quality import check_bars
from tsl.data.store import BarStore
from tsl.data.synthetic import synthetic_bi5, synthetic_ticks

UTC = timezone.utc
EURUSD = SourceSpec("EURUSD", 100000.0, 0.5, 2.0)


# --- URL construction --------------------------------------------------------


@pytest.mark.parametrize(
    "when,expected_month",
    [
        (datetime(2024, 1, 3, 0, tzinfo=UTC), "00"),
        (datetime(2024, 6, 12, 10, tzinfo=UTC), "05"),
        (datetime(2024, 12, 31, 23, tzinfo=UTC), "11"),
    ],
)
def test_month_is_zero_indexed(when, expected_month):
    """The classic Dukascopy mistake. A wrong month fetches real data from the
    wrong period, which no downstream check would catch."""
    assert f"/2024/{expected_month}/" in hour_url("EURUSD", when)


def test_url_shape():
    url = hour_url("XAUUSD", datetime(2023, 3, 7, 9, tzinfo=UTC))
    assert url.endswith("/XAUUSD/2023/02/07/09h_ticks.bi5")


def test_naive_datetime_is_refused():
    with pytest.raises(ValueError, match="timezone-aware"):
        hour_url("EURUSD", datetime(2024, 6, 12, 10))


def test_hours_between_is_half_open():
    start = datetime(2024, 6, 12, 22, tzinfo=UTC)
    end = datetime(2024, 6, 13, 1, tzinfo=UTC)
    assert list(hours_between(start, end)) == [
        datetime(2024, 6, 12, 22, tzinfo=UTC),
        datetime(2024, 6, 12, 23, tzinfo=UTC),
        datetime(2024, 6, 13, 0, tzinfo=UTC),
    ]


# --- decoding ----------------------------------------------------------------


def test_decode_round_trip():
    hour = datetime(2024, 6, 12, 10, tzinfo=UTC)
    ticks = synthetic_ticks(hour, hours=1.0, ticks_per_hour=500, seed=1)
    decoded = decode_hour(synthetic_bi5(ticks, hour, EURUSD.point_scale), EURUSD, hour)

    assert len(decoded) == len(ticks)
    np.testing.assert_allclose(decoded["bid"], ticks["bid"], atol=1e-5)
    np.testing.assert_allclose(decoded["ask"], ticks["ask"], atol=1e-5)
    assert decoded["timestamp"].is_monotonic_increasing


def test_empty_payload_is_a_quiet_hour_not_an_error():
    """Weekends and holidays return zero-length files. Normal."""
    hour = datetime(2024, 6, 15, 3, tzinfo=UTC)
    assert decode_hour(b"", EURUSD, hour).empty


def test_garbage_payload_is_rejected():
    hour = datetime(2024, 6, 12, 10, tzinfo=UTC)
    with pytest.raises(DecodeError, match="not valid LZMA"):
        decode_hour(b"<html>404 not found</html>", EURUSD, hour)


def test_truncated_file_is_rejected():
    hour = datetime(2024, 6, 12, 10, tzinfo=UTC)
    payload = lzma.compress(b"\x00" * 25, format=lzma.FORMAT_ALONE)  # not a multiple of 20
    with pytest.raises(DecodeError, match="whole number"):
        decode_hour(payload, EURUSD, hour)


def test_wrong_point_scale_aborts_loudly():
    """THE SAFETY NET under the unverified scale factors.

    Decoding EURUSD with gold's scale gives prices around 110, far outside the
    configured band. It must raise, not store."""
    hour = datetime(2024, 6, 12, 10, tzinfo=UTC)
    ticks = synthetic_ticks(hour, hours=0.1, ticks_per_hour=100, seed=2)
    payload = synthetic_bi5(ticks, hour, 100000.0)

    wrong = SourceSpec("EURUSD", point_scale=1000.0, plausible_low=0.5, plausible_high=2.0)
    with pytest.raises(DecodeError, match="point_scale"):
        decode_hour(payload, wrong, hour)


def test_swapped_bid_ask_is_detected():
    hour = datetime(2024, 6, 12, 10, tzinfo=UTC)
    ticks = synthetic_ticks(hour, hours=0.1, ticks_per_hour=50, seed=3)
    swapped = ticks.rename(columns={"bid": "ask", "ask": "bid"})
    payload = synthetic_bi5(swapped, hour, EURUSD.point_scale)
    with pytest.raises(DecodeError, match="bid above ask"):
        decode_hour(payload, EURUSD, hour)


# --- bars --------------------------------------------------------------------


def test_bars_are_labelled_by_their_opening_instant():
    """A 15-minute bar stamped 09:15 covers 09:15:00 to 09:29:59.999.

    If bars were stamped by their close, a strategy acting on the 09:15 bar
    would be acting on data from 09:30 - look-ahead bias."""
    start = datetime(2024, 6, 12, 9, 15, tzinfo=UTC)
    ticks = synthetic_ticks(start, hours=0.5, ticks_per_hour=1800, seed=4)
    bars = ticks_to_bars(ticks, "15min")
    assert list(bars.index) == [
        pd.Timestamp("2024-06-12 09:15", tz="UTC"),
        pd.Timestamp("2024-06-12 09:30", tz="UTC"),
    ]


def test_bar_ohlc_matches_the_ticks_inside_it():
    start = datetime(2024, 6, 12, 9, 0, tzinfo=UTC)
    ticks = synthetic_ticks(start, hours=1.0, ticks_per_hour=600, seed=5)
    bars = ticks_to_bars(ticks, "1h")

    mid = (ticks["bid"] + ticks["ask"]) / 2
    row = bars.iloc[0]
    assert row["open"] == pytest.approx(mid.iloc[0])
    assert row["close"] == pytest.approx(mid.iloc[-1])
    assert row["high"] == pytest.approx(mid.max())
    assert row["low"] == pytest.approx(mid.min())
    assert row["tick_count"] == len(ticks)


def test_intervals_with_no_ticks_are_dropped_not_filled():
    """Forward-filling would let a strategy trade a closed market."""
    start = datetime(2024, 6, 12, 9, 0, tzinfo=UTC)
    early = synthetic_ticks(start, hours=0.2, ticks_per_hour=300, seed=6)
    late = synthetic_ticks(start + timedelta(hours=2), hours=0.2, ticks_per_hour=300, seed=7)
    bars = ticks_to_bars(pd.concat([early, late]), "15min")

    assert len(bars) == 2
    assert (bars["tick_count"] > 0).all()
    assert pd.Timestamp("2024-06-12 10:00", tz="UTC") not in bars.index


def test_spread_is_carried_not_baked_into_prices():
    """The cost model applies the spread at fill time, so it must stay separate."""
    start = datetime(2024, 6, 12, 9, 0, tzinfo=UTC)
    ticks = synthetic_ticks(start, hours=0.5, ticks_per_hour=600, spread=0.0002, seed=8)
    bars = ticks_to_bars(ticks, "15min")
    assert bars["spread_mean"].iloc[0] == pytest.approx(0.0002)
    mid = (ticks["bid"] + ticks["ask"]) / 2
    assert bars["close"].iloc[-1] == pytest.approx(mid.iloc[-1])


def test_ticks_missing_a_column_are_refused():
    with pytest.raises(ValueError, match="bid"):
        ticks_to_bars(pd.DataFrame({"timestamp": [], "ask": []}), "1min")


def test_resample_to_coarser_preserves_extremes():
    start = datetime(2024, 6, 12, 9, 0, tzinfo=UTC)
    ticks = synthetic_ticks(start, hours=2.0, ticks_per_hour=1200, seed=9)
    fine = ticks_to_bars(ticks, "5min")
    coarse = resample_bars(fine, "1h")

    assert len(coarse) == 2
    first_hour = fine[fine.index < pd.Timestamp("2024-06-12 10:00", tz="UTC")]
    assert coarse["high"].iloc[0] == pytest.approx(first_hour["high"].max())
    assert coarse["low"].iloc[0] == pytest.approx(first_hour["low"].min())
    assert coarse["tick_count"].iloc[0] == first_hour["tick_count"].sum()


# --- store -------------------------------------------------------------------


def _bars(start, hours=4.0, seed=10):
    return ticks_to_bars(synthetic_ticks(start, hours=hours, ticks_per_hour=600, seed=seed), "15min")


def test_write_then_read_round_trip(tmp_path):
    store = BarStore(tmp_path)
    bars = _bars(datetime(2024, 6, 12, 8, tzinfo=UTC))
    store.write("EURUSD", "15min", bars)

    back = store.read("EURUSD", "15min")
    pd.testing.assert_frame_equal(back, bars, check_freq=False)


def test_rewriting_an_overlapping_range_does_not_duplicate(tmp_path):
    """A download can be interrupted and resumed; re-import must not duplicate."""
    store = BarStore(tmp_path)
    bars = _bars(datetime(2024, 6, 12, 8, tzinfo=UTC))
    store.write("EURUSD", "15min", bars)
    store.write("EURUSD", "15min", bars)

    back = store.read("EURUSD", "15min")
    assert len(back) == len(bars)
    assert not back.index.duplicated().any()


def test_a_rewrite_wins_over_the_stored_copy(tmp_path):
    """Re-downloading is assumed to be a correction."""
    store = BarStore(tmp_path)
    bars = _bars(datetime(2024, 6, 12, 8, tzinfo=UTC))
    store.write("EURUSD", "15min", bars)

    corrected = bars.copy()
    corrected["close"] = 9.9999
    store.write("EURUSD", "15min", corrected)

    assert (store.read("EURUSD", "15min")["close"] == 9.9999).all()


def test_months_are_stored_separately(tmp_path):
    store = BarStore(tmp_path)
    store.write("EURUSD", "15min", _bars(datetime(2024, 6, 28, 20, tzinfo=UTC), hours=8))
    store.write("EURUSD", "15min", _bars(datetime(2024, 7, 1, 8, tzinfo=UTC), hours=8, seed=11))

    files = sorted(p.name for p in (tmp_path / "bars" / "EURUSD" / "15min").glob("*.parquet"))
    assert files == ["2024-06.parquet", "2024-07.parquet"]
    assert len(store.read("EURUSD", "15min")) > 0


def test_read_range_is_half_open(tmp_path):
    store = BarStore(tmp_path)
    bars = _bars(datetime(2024, 6, 12, 8, tzinfo=UTC), hours=4)
    store.write("EURUSD", "15min", bars)

    cut = bars.index[4]
    sliced = store.read("EURUSD", "15min", start=bars.index[2], end=cut)
    assert sliced.index[0] == bars.index[2]
    assert cut not in sliced.index


def test_reading_an_unknown_symbol_is_empty_not_an_error(tmp_path):
    assert BarStore(tmp_path).read("NZDUSD", "15min").empty
    assert BarStore(tmp_path).coverage("NZDUSD", "15min") is None


def test_naive_index_is_refused_on_write(tmp_path):
    bars = _bars(datetime(2024, 6, 12, 8, tzinfo=UTC))
    bars.index = bars.index.tz_localize(None)
    with pytest.raises(ValueError, match="timezone-aware"):
        BarStore(tmp_path).write("EURUSD", "15min", bars)


# --- quality -----------------------------------------------------------------


def test_clean_data_passes():
    bars = _bars(datetime(2024, 6, 12, 8, tzinfo=UTC), hours=12)
    report = check_bars(bars, "EURUSD", "15min")
    assert report.ok, str(report)


def test_empty_series_is_an_error():
    from tsl.data.bars import empty_bars
    assert not check_bars(empty_bars(), "EURUSD", "15min").ok


def test_inconsistent_ohlc_is_an_error():
    bars = _bars(datetime(2024, 6, 12, 8, tzinfo=UTC))
    bars.iloc[3, bars.columns.get_loc("high")] = bars["low"].iloc[3] - 0.01
    report = check_bars(bars, "EURUSD", "15min")
    assert not report.ok
    assert any(f.code == "ohlc_inconsistent" for f in report.errors)


def test_negative_price_is_an_error():
    bars = _bars(datetime(2024, 6, 12, 8, tzinfo=UTC))
    bars.iloc[2, bars.columns.get_loc("low")] = -1.0
    assert any(f.code == "bad_prices" for f in check_bars(bars, "EURUSD", "15min").errors)


def test_saturday_bars_are_an_error():
    """Almost always a timezone bug, which would misplace every session boundary."""
    bars = _bars(datetime(2024, 6, 15, 8, tzinfo=UTC))  # 2024-06-15 is a Saturday
    report = check_bars(bars, "EURUSD", "15min")
    assert any(f.code == "weekend_data" for f in report.errors)


def test_swapped_spread_is_an_error():
    bars = _bars(datetime(2024, 6, 12, 8, tzinfo=UTC))
    bars["spread_mean"] = -0.0001
    assert any(f.code == "negative_spread" for f in check_bars(bars, "EURUSD", "15min").errors)


def test_stalled_feed_is_flagged():
    bars = _bars(datetime(2024, 6, 12, 8, tzinfo=UTC), hours=24)
    bars.iloc[10:60, bars.columns.get_loc("close")] = bars["close"].iloc[10]
    report = check_bars(bars, "EURUSD", "15min")
    assert any(f.code == "flat_price" for f in report.findings)


def test_price_spike_is_flagged_as_a_warning_not_an_error():
    """A genuine Monday gap looks identical to a bad tick, so a human decides."""
    bars = _bars(datetime(2024, 6, 12, 8, tzinfo=UTC), hours=24)
    # Move the whole bar together, so it stays internally consistent and only
    # the spike check has anything to say about it.
    spiked = bars["close"].iloc[40] * 1.5
    for column in ("open", "high", "low", "close"):
        bars.iloc[40, bars.columns.get_loc(column)] = spiked

    report = check_bars(bars, "EURUSD", "15min")
    assert any(f.code == "price_spike" for f in report.findings)
    assert report.ok  # a warning, not an error - a real Monday gap looks the same


def test_midweek_hole_is_reported():
    bars = _bars(datetime(2024, 6, 12, 8, tzinfo=UTC), hours=12)
    holed = pd.concat([bars.iloc[:10], bars.iloc[30:]])
    report = check_bars(holed, "EURUSD", "15min")
    assert any(f.code == "missing_bars" for f in report.findings)


def test_report_renders_readably():
    bars = _bars(datetime(2024, 6, 12, 8, tzinfo=UTC))
    bars.iloc[2, bars.columns.get_loc("low")] = -1.0
    text = str(check_bars(bars, "EURUSD", "15min"))
    assert "EURUSD 15min" in text and "ERROR" in text
