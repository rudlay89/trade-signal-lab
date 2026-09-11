"""Daily candle files - the low-volume download path.

The record layout here is assumed rather than known, so these tests concentrate
on the ways a wrong assumption shows itself: an impossible time offset, prices
outside a plausible band, and above all a field order that stops "high" being
the highest price.
"""

from datetime import datetime, timezone

import lzma
import pandas as pd
import pytest

from tsl.data.bars import ticks_to_bars
from tsl.data.candles import (
    candle_url, candles_to_bars, compare_bar_series, days_between, decode_candles,
)
from tsl.data.dukascopy import DecodeError, SourceSpec
from tsl.data.synthetic import synthetic_candle_file, synthetic_ticks

UTC = timezone.utc
DAY = datetime(2024, 1, 8, tzinfo=UTC)
GOLD = SourceSpec("XAUUSD", 1000.0, 500.0, 10000.0)


def _gold_bars(hours=4, spread=0.30, seed=1):
    ticks = synthetic_ticks(
        DAY, hours=hours, ticks_per_hour=600, start_price=2030.0,
        spread=spread, volatility=0.04, seed=seed,
    )
    return ticks_to_bars(ticks, "1min")


# --- URLs --------------------------------------------------------------------


def test_candle_url_shape():
    assert candle_url("XAUUSD", DAY, "BID").endswith("/XAUUSD/2024/00/08/BID_candles_min_1.bi5")
    assert candle_url("XAUUSD", DAY, "ASK").endswith("/XAUUSD/2024/00/08/ASK_candles_min_1.bi5")


def test_candle_url_month_is_zero_indexed():
    assert "/2024/05/" in candle_url("EURUSD", datetime(2024, 6, 12, tzinfo=UTC), "BID")


def test_unknown_side_is_refused():
    with pytest.raises(ValueError, match="side must be"):
        candle_url("EURUSD", DAY, "MID")


def test_days_between_is_half_open():
    days = list(days_between(DAY, datetime(2024, 1, 11, tzinfo=UTC)))
    assert [d.day for d in days] == [8, 9, 10]


# --- decoding ----------------------------------------------------------------


def test_decode_round_trip():
    bars = _gold_bars()
    decoded = decode_candles(synthetic_candle_file(bars, DAY, GOLD.point_scale), GOLD, DAY, "BID")

    assert len(decoded) == len(bars)
    for column in ("open", "high", "low", "close"):
        # check_freq=False: tick-derived bars carry an inferred frequency on the
        # index, decoded candles do not. That is metadata, not data.
        pd.testing.assert_series_equal(
            decoded[column], bars[column], check_names=False, check_freq=False, rtol=1e-6
        )


def test_empty_payload_is_a_closed_market():
    assert decode_candles(b"", GOLD, DAY, "BID").empty


def test_truncated_file_names_the_layout_as_a_suspect():
    payload = lzma.compress(b"\x00" * 30, format=lzma.FORMAT_ALONE)  # not a multiple of 24
    with pytest.raises(DecodeError, match="record layout"):
        decode_candles(payload, GOLD, DAY, "BID")


def test_the_layout_actually_used_is_recorded_not_assumed():
    bars = _gold_bars()
    decoded = decode_candles(synthetic_candle_file(bars, DAY, GOLD.point_scale), GOLD, DAY, "BID")
    assert decoded.attrs["field_order"] == ("open", "close", "low", "high")


def test_each_candidate_order_is_recognised():
    """Both orders decode correctly when the data really is in that order. The
    decoder eliminates the impossible rather than insisting on one answer."""
    bars = _gold_bars()
    for order in (("open", "close", "low", "high"), ("open", "high", "low", "close")):
        payload = synthetic_candle_file(bars, DAY, GOLD.point_scale, field_order=order)
        decoded = decode_candles(payload, GOLD, DAY, "BID")
        pd.testing.assert_series_equal(
            decoded["high"], bars["high"], check_names=False, check_freq=False, rtol=1e-4
        )


def test_an_explicit_field_order_that_is_wrong_is_refused():
    """Pinning a layout means it must validate, not be taken on trust."""
    bars = _gold_bars()
    payload = synthetic_candle_file(bars, DAY, GOLD.point_scale,
                                    field_order=("open", "close", "low", "high"))
    with pytest.raises(DecodeError, match="no candidate field order"):
        decode_candles(payload, GOLD, DAY, "BID",
                       field_order=("open", "high", "low", "close"))


def test_float_prices_are_rejected():
    """REGRESSION. The first version of this module assumed float32 prices. Real
    files use int32 scaled by point_scale, and because both records are 24 bytes
    the size check passed and gold decoded as a 2.8e-39 denormal.
    """
    bars = _gold_bars()
    payload = synthetic_candle_file(bars, DAY, GOLD.point_scale, as_floats=True)
    with pytest.raises(DecodeError) as exc:
        decode_candles(payload, GOLD, DAY, "BID")
    assert "no candidate field order" in str(exc.value)
    assert "point_scale" in str(exc.value)


def test_prices_outside_the_plausible_band_are_refused():
    bars = _gold_bars()
    for column in ("open", "high", "low", "close"):
        bars[column] = bars[column] / 1000.0
    payload = synthetic_candle_file(bars, DAY, GOLD.point_scale)
    with pytest.raises(DecodeError, match="plausible range"):
        decode_candles(payload, GOLD, DAY, "BID")


def test_impossible_time_offset_is_refused():
    import struct

    body = struct.pack(">5if", 999_999, 2030000, 2031000, 2029000, 2032000, 1.0)
    payload = lzma.compress(body, format=lzma.FORMAT_ALONE)
    with pytest.raises(DecodeError, match="outside the day"):
        decode_candles(payload, GOLD, DAY, "BID")


# --- combining both sides ----------------------------------------------------


def test_bid_and_ask_combine_into_mid_bars():
    bars = _gold_bars()
    bid, ask = bars.copy(), bars.copy()
    for column in ("open", "high", "low", "close"):
        bid[column] = bars[column] - 0.15
        ask[column] = bars[column] + 0.15

    combined = candles_to_bars(bid, ask)
    assert combined["close"].iloc[0] == pytest.approx(bars["close"].iloc[0])
    assert combined["spread_mean"].iloc[0] == pytest.approx(0.30)


def test_combined_bars_have_the_same_columns_as_tick_bars():
    """Downstream code must not be able to tell which source a series came from."""
    from tsl.data.bars import BAR_COLUMNS

    bars = _gold_bars()
    combined = candles_to_bars(bars.copy(), bars.copy())
    assert set(BAR_COLUMNS) == set(combined.columns)


def test_one_missing_side_yields_nothing():
    from tsl.data.bars import empty_bars

    assert candles_to_bars(_gold_bars(), empty_bars()).empty


# --- the comparison that proves the format -----------------------------------


def test_matching_series_pass_the_comparison():
    bars = _gold_bars()
    bid, ask = bars.copy(), bars.copy()
    for column in ("open", "high", "low", "close"):
        bid[column] = bars[column] - 0.15
        ask[column] = bars[column] + 0.15

    result = compare_bar_series(bars, candles_to_bars(bid, ask))
    assert result["passed"], result
    assert "MATCH" in result["verdict"]
    assert result["overlapping"] == len(bars)


def test_a_shifted_series_fails_the_comparison():
    """If the candle path produced prices out by even a few spreads, the
    comparison must say so rather than shrug."""
    bars = _gold_bars()
    shifted = bars.copy()
    for column in ("open", "high", "low", "close"):
        shifted[column] = bars[column] + 5.0

    result = compare_bar_series(bars, shifted)
    assert not result["passed"]
    assert "MISMATCH" in result["verdict"]


def test_no_overlap_is_reported_not_silently_passed():
    bars = _gold_bars()
    other = bars.copy()
    other.index = other.index + pd.Timedelta(days=30)
    result = compare_bar_series(bars, other)
    assert result["overlapping"] == 0
    assert "passed" not in result


# --- the verify-candles command ----------------------------------------------


@pytest.fixture
def stored_tick_bars(tmp_path):
    from tsl.data.store import BarStore

    bars = _gold_bars(hours=6)
    BarStore(tmp_path).write("XAUUSD", "1min", bars)
    return bars


def test_verify_reports_a_match(monkeypatch, stored_tick_bars, tmp_path, capsys):
    from tsl.cli import main
    from tsl.data import candles

    def fake_day(session, spec, day, **kwargs):
        bid, ask = stored_tick_bars.copy(), stored_tick_bars.copy()
        for column in ("open", "high", "low", "close"):
            bid[column] = stored_tick_bars[column] - 0.15
            ask[column] = stored_tick_bars[column] + 0.15
        return candles.candles_to_bars(bid, ask)

    monkeypatch.setattr("tsl.data.candles.download_day_candles", fake_day)
    assert main(["--data", str(tmp_path), "verify-candles", "XAUUSD"]) == 0

    out = capsys.readouterr().out
    assert "MATCH" in out
    assert "twelve times fewer requests" in out


def test_verify_refuses_to_bless_a_mismatch(monkeypatch, stored_tick_bars, tmp_path, capsys):
    from tsl.cli import main

    def fake_day(session, spec, day, **kwargs):
        wrong = stored_tick_bars.copy()
        for column in ("open", "high", "low", "close"):
            wrong[column] = stored_tick_bars[column] + 7.0
        return wrong

    monkeypatch.setattr("tsl.data.candles.download_day_candles", fake_day)
    assert main(["--data", str(tmp_path), "verify-candles", "XAUUSD"]) == 2

    out = capsys.readouterr().out
    assert "MISMATCH" in out
    assert "field order" in out
    assert "Do not use candle downloads" in out


def test_verify_without_tick_data_says_what_to_run(tmp_path, capsys):
    from tsl.cli import main

    assert main(["--data", str(tmp_path), "verify-candles", "XAUUSD"]) == 1
    assert "download XAUUSD" in capsys.readouterr().out


# --- spread, which the cost model depends on ---------------------------------


def _sided(bars, half_spread, high_extra=0.0):
    """Build bid/ask candle frames around mid bars, optionally widening the
    spread at the highs so the max-spread estimate has something to find."""
    bid, ask = bars.copy(), bars.copy()
    for column in ("open", "high", "low", "close"):
        bid[column] = bars[column] - half_spread
        ask[column] = bars[column] + half_spread
    ask["high"] = ask["high"] + high_extra
    bid["high"] = bid["high"] - high_extra
    return bid, ask


def test_spread_mean_comes_from_the_open_and_close_quotes():
    bars = _gold_bars(hours=1)
    combined = candles_to_bars(*_sided(bars, 0.15))
    assert combined["spread_mean"].iloc[0] == pytest.approx(0.30)


def test_spread_max_is_a_spread_not_the_bar_range():
    """REGRESSION. This was computed as high_ask - low_bid, which is the bar's
    range plus a spread. On gold that reads about $2.30 where the real spread is
    $0.33 - every bar a false blowout, and costs overstated sixfold."""
    bars = _gold_bars(hours=2)
    bid, ask = _sided(bars, 0.15)
    combined = candles_to_bars(bid, ask)

    # The spread is a constant 0.30 here, so that is the only correct answer.
    assert combined["spread_max"].max() == pytest.approx(0.30)

    # And it must not be what the old formula gave, which folded in the range.
    buggy = (ask["high"] - bid["low"]).rename("buggy")
    assert (buggy > 0.30).mean() > 0.9, "test data must have real range to be meaningful"
    assert (combined["spread_max"] < buggy).mean() > 0.9


def test_spread_max_finds_a_genuine_widening():
    bars = _gold_bars(hours=1)
    combined = candles_to_bars(*_sided(bars, 0.15, high_extra=0.5))
    assert combined["spread_max"].iloc[0] == pytest.approx(1.30)
    assert combined["spread_mean"].iloc[0] == pytest.approx(0.30)


def test_spread_is_never_negative():
    bars = _gold_bars(hours=1)
    combined = candles_to_bars(*_sided(bars, 0.15))
    assert (combined["spread_mean"] >= 0).all()
    assert (combined["spread_max"] >= 0).all()


def test_candle_bars_pass_the_quality_checks():
    """The false-blowout bug would have shown up here."""
    from tsl.data.quality import check_bars

    bars = _gold_bars(hours=8)
    combined = candles_to_bars(*_sided(bars, 0.165))
    report = check_bars(combined, "XAUUSD", "1min")

    assert report.ok, str(report)
    assert not any(f.code == "spread_blowout" for f in report.findings), str(report)
