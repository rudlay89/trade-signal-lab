"""Padded candle minutes.

Dukascopy candle files carry a record for every minute of the day, including
minutes when nothing traded. Those are placeholders, and leaving them in would
fabricate an overnight range on days the market was shut - the exact input the
London breakout strategy reads.
"""

from datetime import datetime, timezone

import pandas as pd
import pytest

from tsl.data.bars import ticks_to_bars
from tsl.data.candles import candles_to_bars, compare_bar_series
from tsl.data.padding import drop_padding, padded_mask, padding_report
from tsl.data.synthetic import pad_bars, synthetic_ticks

UTC = timezone.utc
DAY = datetime(2024, 1, 8, tzinfo=UTC)          # a Monday
SATURDAY = datetime(2024, 1, 6, tzinfo=UTC)


def _real_bars(day=DAY, hours=4):
    ticks = synthetic_ticks(
        day, hours=hours, ticks_per_hour=600, start_price=2030.0,
        spread=0.30, volatility=0.04, seed=1,
    )
    return ticks_to_bars(ticks, "1min")


# --- detection ---------------------------------------------------------------


def test_padded_minutes_are_identified():
    real = _real_bars(hours=4)
    padded = pad_bars(real, DAY)

    mask = padded_mask(padded)
    assert mask.sum() == 1440 - len(real)
    assert not mask.loc[real.index].any(), "no real bar may be marked as padding"


def test_a_flat_bar_with_volume_is_kept():
    """A genuine single-tick minute is flat but traded. Requiring both signatures
    keeps it - dropping real bars is worse than keeping a few fake ones, because
    a hole is silent while a kept bar is visible to the gap checks."""
    bars = _real_bars(hours=1)
    idx = bars.index[5]
    for column in ("open", "high", "low", "close"):
        bars.loc[idx, column] = 2030.0
    bars.loc[idx, "volume"] = 3.5

    assert not padded_mask(bars).loc[idx]


def test_a_zero_volume_bar_that_moved_is_kept():
    bars = _real_bars(hours=1)
    bars.loc[bars.index[5], "volume"] = 0.0
    assert not padded_mask(bars).loc[bars.index[5]]


def test_drop_padding_reports_how_many_went():
    real = _real_bars(hours=4)
    cleaned, dropped = drop_padding(pad_bars(real, DAY))

    assert dropped == 1440 - len(real)
    assert len(cleaned) == len(real)
    pd.testing.assert_index_equal(cleaned.index, real.index)


def test_empty_input_is_handled():
    from tsl.data.bars import empty_bars

    cleaned, dropped = drop_padding(empty_bars())
    assert cleaned.empty and dropped == 0


# --- the report that justifies the filter ------------------------------------


def test_report_counts_both_signatures():
    real = _real_bars(hours=4)
    report = padding_report(pad_bars(real, DAY))

    assert report.total == 1440
    assert report.both == 1440 - len(real)
    assert report.signatures_agree


def test_report_flags_disagreeing_signatures():
    """If the two signatures pick out different bars, the assumption about what
    padding looks like is wrong and should be revisited, not filtered on."""
    real = _real_bars(hours=4)
    padded = pad_bars(real, DAY)
    padded.loc[real.index[0], "volume"] = 0.0    # zero volume but real movement

    report = padding_report(padded)
    assert not report.signatures_agree


def test_report_counts_weekend_bars():
    """A Saturday bar is proof on its own that the feed pads."""
    real = _real_bars(day=SATURDAY, hours=4)
    report = padding_report(pad_bars(real, SATURDAY))

    assert report.weekend == 1440
    assert report.by_weekday["Sat"] == 1440
    assert report.by_weekday["Mon"] == 0


def test_report_renders_readably():
    text = str(padding_report(pad_bars(_real_bars(), DAY)))
    assert "zero volume" in text and "bars per weekday" in text


# --- the candle path drops padding automatically -----------------------------


def test_candles_to_bars_strips_padding():
    real = _real_bars(hours=4)
    bid, ask = pad_bars(real, DAY).copy(), pad_bars(real, DAY).copy()
    for column in ("open", "high", "low", "close"):
        bid[column] = bid[column] - 0.15
        ask[column] = ask[column] + 0.15

    combined = candles_to_bars(bid, ask)
    assert len(combined) == len(real)
    assert combined.attrs["padded_dropped"] == 1440 - len(real)


def test_a_fully_padded_day_yields_nothing():
    """A weekend file is entirely placeholder. It must produce no bars at all."""
    from tsl.data.bars import empty_bars

    real = _real_bars(hours=4)
    skeleton = pad_bars(real, DAY)
    for column in ("open", "high", "low", "close"):
        skeleton[column] = 2030.0
    skeleton["volume"] = 0.0

    assert candles_to_bars(skeleton.copy(), skeleton.copy()).empty


# --- the blind spot that let this through ------------------------------------


def test_comparison_now_reports_minutes_the_other_source_lacks():
    """REGRESSION. compare_bar_series looked only at shared minutes, so a candle
    series with 60 fabricated extra minutes matched perfectly on the 1,380 they
    had in common and the padding went unseen."""
    real = _real_bars(hours=4)
    padded = pad_bars(real, DAY)
    for column in ("open", "high", "low", "close"):
        padded[column] = padded[column]
    padded["spread_mean"] = 0.30

    result = compare_bar_series(real, padded)
    assert result["only_in_candles"] == 1440 - len(real)
    assert "check for padding" in result["verdict"]


def test_a_clean_comparison_says_nothing_about_padding():
    real = _real_bars(hours=4)
    same = real.copy()
    result = compare_bar_series(real, same)
    assert result["only_in_candles"] == 0
    assert "padding" not in result["verdict"]


# --- the daily rollover break is expected, not a fault -----------------------


def _week_of_bars(break_hour=23):
    """Five weekdays of 1-minute bars with an hour-long daily break, as the
    real feed delivers once padding is stripped."""
    frames = []
    for day_offset in range(5):
        day = datetime(2024, 1, 8, tzinfo=UTC) + pd.Timedelta(days=day_offset)
        ticks = synthetic_ticks(
            day, hours=break_hour, ticks_per_hour=120, start_price=2030.0,
            spread=0.30, volatility=0.02, seed=day_offset,
        )
        frames.append(ticks_to_bars(ticks, "1min"))
    return pd.concat(frames)


def test_the_daily_break_is_reported_as_expected_not_a_warning():
    """REGRESSION. Every trading day has an hour when the venue closes. Flagged
    as a gap, that is ~250 warnings a year for normal market structure, which
    trains the reader to ignore the report."""
    from tsl.data.quality import check_bars

    report = check_bars(_week_of_bars(), "XAUUSD", "1min")

    daily = [f for f in report.findings if f.code == "daily_break"]
    assert daily, str(report)
    assert daily[0].severity == "info"
    assert "expected" in daily[0].message

    assert not [f for f in report.findings if f.code == "missing_bars"], str(report)
    assert report.ok


def test_a_real_hole_is_still_reported():
    """The daily break must not become a blanket excuse for missing data."""
    from tsl.data.quality import check_bars

    bars = _week_of_bars()
    midday = pd.Timestamp("2024-01-10 12:00", tz="UTC")
    holed = bars[(bars.index < midday) | (bars.index > midday + pd.Timedelta(hours=2))]

    report = check_bars(holed, "XAUUSD", "1min")
    missing = [f for f in report.findings if f.code == "missing_bars"]
    assert missing, str(report)


def test_scattered_gaps_are_not_mistaken_for_a_daily_break():
    """Random holes do not concentrate on one hour, so they stay warnings."""
    from tsl.data.quality import check_bars

    bars = _week_of_bars()
    drop = bars.index[[500, 1500, 2600, 3700, 4800]]
    holed = bars.drop(
        [ts for start in drop for ts in pd.date_range(start, periods=30, freq="1min")],
        errors="ignore",
    )

    report = check_bars(holed, "XAUUSD", "1min")
    assert [f for f in report.findings if f.code == "missing_bars"], str(report)


def _year_with_dst_shift(shift_after="2024-01-29"):
    """A continuous minute series with the weekend and the daily rollover break
    cut out of it, where the break moves by an hour partway through - as it
    really does when daylight saving changes.

    Built by removal rather than by generating each day separately, so the gaps
    land where real gaps land: trading resumes at 23:00 UTC before the shift and
    22:00 after it.
    """
    index = pd.date_range("2024-01-08", "2024-02-16", freq="1min", tz="UTC")
    frame = pd.DataFrame(index=index)

    weekend = index.dayofweek >= 5
    before = index < pd.Timestamp(shift_after, tz="UTC")
    # Winter: closed 22:00-23:00, so trading resumes at 23:00.
    # Summer: closed 21:00-22:00, so trading resumes at 22:00.
    in_break = ((before & (index.hour == 22)) | (~before & (index.hour == 21)))

    keep = index[~weekend & ~in_break]
    bars = pd.DataFrame(
        {
            "open": 2030.0, "high": 2030.5, "low": 2029.5, "close": 2030.2,
            "spread_mean": 0.30, "spread_max": 0.35, "tick_count": 10, "volume": 5.0,
        },
        index=keep,
    )
    bars.index.name = "timestamp"
    return bars


def test_a_daylight_saving_shift_does_not_become_missing_data():
    """REGRESSION. The break's wall-clock hour moves with US daylight saving. A
    year of real gold gave 135 breaks resuming at 22:00 UTC and 75 at 23:00; a
    detector assuming one hour reported the entire winter as missing bars."""
    from tsl.data.quality import check_bars

    report = check_bars(_year_with_dst_shift(), "XAUUSD", "1min")

    daily = [f for f in report.findings if f.code == "daily_break"]
    assert daily, str(report)
    assert "22:00" in daily[0].message and "23:00" in daily[0].message
    assert "daylight saving" in daily[0].message

    assert not [f for f in report.findings if f.code == "missing_bars"], str(report)


def test_a_real_hole_still_surfaces_alongside_two_break_hours():
    """The break must not become a blanket excuse for missing data."""
    from tsl.data.quality import check_bars

    bars = _year_with_dst_shift()
    midday = pd.Timestamp("2024-01-17 11:00", tz="UTC")
    holed = bars[(bars.index < midday) | (bars.index > midday + pd.Timedelta(hours=2))]

    report = check_bars(holed, "XAUUSD", "1min")
    assert [f for f in report.findings if f.code == "daily_break"], str(report)
    assert [f for f in report.findings if f.code == "missing_bars"], str(report)
