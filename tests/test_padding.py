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
