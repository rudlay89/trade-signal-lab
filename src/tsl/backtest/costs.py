"""The cost model.

This is the most important module in the backtest, and the easiest one to get
flatteringly wrong. On a 20-pip intraday trade a 1.5-pip round trip is 7.5% of
the move. A strategy with a real edge before costs can be a losing strategy
after them, so the costs must be modelled from measurements rather than hope.

Three components:

  SPREAD      Taken from the DATA, per bar, not from a config constant. Every
              stored bar carries the spread that actually prevailed. A fixed
              assumption understates costs exactly when it matters most - around
              news, at the session open, on the volatile days a breakout system
              wants to trade. A year of XAUUSD had a median spread of 0.38 and a
              maximum of 5.98: a single number cannot represent that.

  SLIPPAGE    Stop orders do not fill at their trigger price. Modelled as a
              fraction of the spread, applied always against the trader.

  FINANCING   Not modelled, because nothing is held overnight. If that ever
              changes this module must change with it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        return 1 if self is Side.LONG else -1

    @property
    def opposite(self) -> "Side":
        return Side.SHORT if self is Side.LONG else Side.LONG


class OrderKind(str, Enum):
    MARKET = "market"
    STOP = "stop"      # triggers when price moves THROUGH the level, then slips
    LIMIT = "limit"    # fills at the level or better, no slippage


@dataclass(frozen=True)
class CostModel:
    """How a mid price becomes a fill price.

    `slippage_spreads` is in units of the prevailing spread, so slippage widens
    automatically in the conditions where it really does widen.
    """

    slippage_spreads: float = 0.5
    commission_per_lot: float = 0.0
    fallback_spread: float = 0.0

    def half_spread(self, spread: float | None) -> float:
        """Half the prevailing spread - the distance from mid to bid or ask."""
        if spread is None or spread != spread or spread < 0:   # None or NaN
            spread = self.fallback_spread
        return spread / 2.0

    def fill_price(
        self,
        mid: float,
        side: Side,
        kind: OrderKind,
        spread: float | None,
        closing: bool = False,
    ) -> float:
        """The price actually paid or received.

        Opening a long buys at the ask (mid + half spread); closing it sells at
        the bid. A short is the mirror. Slippage is added for stop orders and
        always pushes the fill against the trader - a stop-loss fills worse, and
        a breakout entry fills higher than its trigger.
        """
        half = self.half_spread(spread)
        # Buying when opening a long or closing a short; selling otherwise.
        buying = (side is Side.LONG) != closing
        price = mid + half if buying else mid - half

        if kind is OrderKind.STOP:
            slip = half * 2.0 * self.slippage_spreads
            price += slip if buying else -slip
        return price

    def round_trip_cost(self, spread: float | None, lots: float) -> float:
        """Commission for one complete trade. Spread cost is in the fill prices."""
        return 2.0 * self.commission_per_lot * lots
