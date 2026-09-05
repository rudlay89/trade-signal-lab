"""Position sizing, and the guard rail that refuses trades it cannot size safely.

The rule this module exists to enforce: never risk more than the budget. Brokers
have minimum position sizes, and on some instruments the minimum already risks
more than 1% of a $1,000 account. The tempting behaviour is to round up and take
the trade anyway. That silently converts a 1% system into a 1.5% system on
exactly the instruments and days where volatility is highest.

So when the smallest position the broker will accept risks more than the budget,
the trade is REJECTED, with a reason recorded. A skipped trade costs nothing. An
oversized one costs the difference every time it loses.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from .instruments import InstrumentSpec


class RejectReason(str, Enum):
    """Why a trade could not be sized. Recorded so rejections are auditable."""

    NOT_TRADEABLE = "instrument is not tradeable"
    ZERO_STOP_DISTANCE = "entry and stop are the same price"
    BELOW_MINIMUM_LOT = "minimum position size risks more than the budget"


@dataclass(frozen=True)
class SizingResult:
    """The outcome of sizing one trade.

    `lots` is zero when `rejected` is true. Callers must check `rejected` rather
    than testing `lots > 0`, so the reason survives into the log.
    """

    symbol: str
    lots: Decimal
    risk_amount: Decimal          # account currency actually at risk
    risk_budget: Decimal          # what we were allowed to risk
    stop_distance: Decimal        # in price units
    rejected: bool = False
    reason: RejectReason | None = None
    detail: str = ""

    @property
    def accepted(self) -> bool:
        return not self.rejected

    def describe(self) -> str:
        """One line for the log or the alert."""
        if self.rejected:
            return f"{self.symbol}: SKIPPED - {self.detail or (self.reason.value if self.reason else '')}"
        pct = (self.risk_amount / self.risk_budget * 100) if self.risk_budget else Decimal(0)
        return (
            f"{self.symbol}: {self.lots} lots, risking {self.risk_amount:.2f} "
            f"of {self.risk_budget:.2f} budget ({pct:.0f}%)"
        )


@dataclass(frozen=True)
class RiskConfig:
    """Account and per-trade risk settings."""

    account_currency: str
    equity: Decimal
    risk_pct: Decimal
    max_budget_overshoot: Decimal
    max_open_positions: int
    max_positions_per_currency: int
    daily_stop_r: Decimal

    @property
    def risk_budget(self) -> Decimal:
        """Account-currency amount risked per trade."""
        return (self.equity * self.risk_pct / Decimal(100)).quantize(Decimal("0.01"))


def load_risk_config(path: str | Path) -> RiskConfig:
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text())
    account = raw["account"]
    per_trade = raw["per_trade"]
    portfolio = raw["portfolio"]

    cfg = RiskConfig(
        account_currency=account["currency"],
        equity=Decimal(str(account["equity"])),
        risk_pct=Decimal(str(per_trade["risk_pct"])),
        max_budget_overshoot=Decimal(str(per_trade.get("max_budget_overshoot", 1.0))),
        max_open_positions=int(portfolio["max_open_positions"]),
        max_positions_per_currency=int(portfolio["max_positions_per_currency"]),
        daily_stop_r=Decimal(str(portfolio["daily_stop_r"])),
    )

    if cfg.equity <= 0:
        raise ValueError("account.equity must be positive")
    if not (0 < cfg.risk_pct <= 100):
        raise ValueError("per_trade.risk_pct must be between 0 and 100")
    if cfg.max_budget_overshoot < 1:
        raise ValueError(
            "per_trade.max_budget_overshoot below 1.0 would reject trades that fit "
            "the budget exactly; use 1.0 for no overshoot"
        )
    if cfg.daily_stop_r >= 0:
        raise ValueError("portfolio.daily_stop_r is a loss limit and must be negative")
    return cfg


# Lot counts are snapped to this precision before being floored to a lot step.
#
# Why: a division whose true answer is exactly 0.06 can come back as
# 0.05999999999999999999999999999 when the divisor repeats (USDJPY divides by its
# own price). Flooring that to a 0.01 step gives 0.05 - a whole step lost, a sixth
# of the intended position, from a rounding artefact.
#
# Snapping first costs at most 5e-13 lots of extra size, which on any instrument
# here is a fraction of a nanodollar of extra risk. Losing a lot step is worth far
# more than that.
_LOT_EPSILON = Decimal("1E-12")


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Round down to a whole number of lot steps.

    Down, never up: rounding up would breach the risk budget, which is the one
    thing this module must not do. The epsilon snap above happens first, so only
    genuine excess is discarded, not floating-point dust.
    """
    snapped = value.quantize(_LOT_EPSILON, rounding=ROUND_HALF_UP)
    return (snapped / step).to_integral_value(rounding=ROUND_DOWN) * step


def size_position(
    spec: InstrumentSpec,
    config: RiskConfig,
    entry: Decimal,
    stop: Decimal,
    price: Decimal | None = None,
) -> SizingResult:
    """Work out how many lots to trade, or refuse the trade.

    `price` is only needed for instruments whose quote currency is not the account
    currency (USDJPY on a USD account); it is the current rate used to convert.
    """
    budget = config.risk_budget
    stop_distance = abs(entry - stop)

    def reject(reason: RejectReason, detail: str) -> SizingResult:
        return SizingResult(
            symbol=spec.symbol,
            lots=Decimal(0),
            risk_amount=Decimal(0),
            risk_budget=budget,
            stop_distance=stop_distance,
            rejected=True,
            reason=reason,
            detail=detail,
        )

    if not spec.tradeable:
        return reject(
            RejectReason.NOT_TRADEABLE,
            spec.tradeable_note or f"{spec.symbol} is configured as not tradeable",
        )

    if stop_distance == 0:
        return reject(
            RejectReason.ZERO_STOP_DISTANCE,
            "entry and stop are identical, so risk per lot is undefined",
        )

    # Money at risk per 1.00 lot, in the account currency.
    risk_per_lot = stop_distance * spec.value_per_price_unit(
        Decimal(1), config.account_currency, price
    )

    raw_lots = budget / risk_per_lot
    lots = _floor_to_step(raw_lots, spec.lot_step)

    if lots < spec.min_lot:
        # The smallest position the broker accepts. Does it fit the budget?
        min_risk = spec.min_lot * risk_per_lot
        allowance = budget * config.max_budget_overshoot

        if min_risk <= allowance:
            lots = spec.min_lot
        else:
            return reject(
                RejectReason.BELOW_MINIMUM_LOT,
                (
                    f"stop is {stop_distance} wide; the minimum {spec.min_lot} lot "
                    f"position would risk {min_risk:.2f}, over the {budget:.2f} budget"
                ),
            )

    return SizingResult(
        symbol=spec.symbol,
        lots=lots,
        risk_amount=(lots * risk_per_lot).quantize(Decimal("0.01")),
        risk_budget=budget,
        stop_distance=stop_distance,
    )
