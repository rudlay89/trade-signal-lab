"""Detecting and removing padded candle minutes.

Dukascopy's daily candle files appear to carry a record for EVERY minute of the
day, including minutes when nothing traded - the daily rollover break, and whole
weekends. Tick files do not: the same gold day yields 1,380 bars from ticks
(23 x 60) but 1,440 from candles (24 x 60), and a Saturday yields a full 1,440.

Those extra records are not market data. Left in place they would:

  * fabricate an overnight range on days the market was shut, which is precisely
    the input the London breakout strategy depends on;
  * flatten ATR and every other volatility measure, by averaging in minutes that
    could not have moved;
  * let a backtest "trade" a closed market.

`verify-candles` did not catch this because it compares only the minutes the two
sources share. The padding is exactly the minutes they do not share.

DETECTION. A padded minute should have no volume and no movement. Both signals
are checked, and `padding_report` exists so the signature can be confirmed
against real data rather than assumed - if the two disagree, the assumption is
wrong and should be revisited before filtering anything.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class PaddingReport:
    """What the padding signatures actually find in a real series."""

    total: int
    zero_volume: int
    flat: int
    both: int
    either: int
    weekend: int
    weekend_both: int
    by_weekday: dict

    @property
    def signatures_agree(self) -> bool:
        """True when 'no volume' and 'no movement' pick out the same bars.

        Disagreement means the assumption about what padding looks like is wrong,
        and filtering on it would remove real bars or keep fake ones.
        """
        return self.both == self.either

    def __str__(self) -> str:
        def pct(n):
            return f"{n:>7,} ({100 * n / self.total:5.1f}%)" if self.total else "0"

        lines = [
            f"  total bars           {self.total:>7,}",
            f"  zero volume          {pct(self.zero_volume)}",
            f"  flat (o=h=l=c)       {pct(self.flat)}",
            f"  both signatures      {pct(self.both)}",
            f"  either signature     {pct(self.either)}",
            f"  on a weekend         {pct(self.weekend)}",
            f"  weekend AND padded   {pct(self.weekend_both)}",
            "",
            "  bars per weekday (a normal week should be flat Mon-Fri, ~0 Sat/Sun):",
        ]
        for name in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
            lines.append(f"    {name}  {self.by_weekday.get(name, 0):>8,}")
        return "\n".join(lines)


def padded_mask(bars: pd.DataFrame) -> pd.Series:
    """True where a bar looks like a placeholder rather than real trading.

    Requires BOTH signatures: no volume and no price movement. Demanding both is
    deliberately conservative - a genuine single-tick minute is flat but carries
    volume, and dropping real bars is worse than keeping a few fake ones, because
    a dropped bar leaves a hole the gap checks will report while a kept one is
    silent.
    """
    if bars.empty:
        return pd.Series([], dtype=bool, index=bars.index)

    flat = (
        (bars["open"] == bars["high"])
        & (bars["high"] == bars["low"])
        & (bars["low"] == bars["close"])
    )
    if "volume" not in bars:
        return flat
    return flat & (bars["volume"] <= 0)


def padding_report(bars: pd.DataFrame) -> PaddingReport:
    """Measure both padding signatures, so the assumption can be checked."""
    if bars.empty:
        return PaddingReport(0, 0, 0, 0, 0, 0, 0, {})

    flat = (
        (bars["open"] == bars["high"])
        & (bars["high"] == bars["low"])
        & (bars["low"] == bars["close"])
    )
    zero_volume = (bars["volume"] <= 0) if "volume" in bars else pd.Series(False, index=bars.index)
    weekend = bars.index.dayofweek >= 5

    names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    by_weekday = {
        names[day]: int((bars.index.dayofweek == day).sum()) for day in range(7)
    }

    return PaddingReport(
        total=len(bars),
        zero_volume=int(zero_volume.sum()),
        flat=int(flat.sum()),
        both=int((flat & zero_volume).sum()),
        either=int((flat | zero_volume).sum()),
        weekend=int(weekend.sum()),
        weekend_both=int((flat & zero_volume & weekend).sum()),
        by_weekday=by_weekday,
    )


def drop_padding(bars: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Remove padded minutes. Returns the cleaned frame and how many went."""
    if bars.empty:
        return bars, 0
    mask = padded_mask(bars)
    return bars[~mask], int(mask.sum())
