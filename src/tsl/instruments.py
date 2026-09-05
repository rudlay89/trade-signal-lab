"""Instrument specifications and the price-move-to-money conversion.

Everything about position sizing rests on one question: if the price moves by D,
how much money is that? Get it wrong and every position size is wrong, silently.
So the conversion lives here, on its own, with worked examples in the tests.

Money and prices use Decimal rather than float. Float rounding on a 5-decimal FX
price is small, but it compounds through the sizing arithmetic, and "small error
in the position size" is not a category of bug worth having. Bulk analytics
(indicators, backtest bars) use float via pandas, which is fine - the Decimal
boundary is at the point where a signal turns into a position.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

# Fields every instrument must define. Anything missing is a config error, raised
# at load time rather than discovered halfway through a backtest.
_REQUIRED = (
    "kind",
    "base",
    "quote",
    "contract_size",
    "min_lot",
    "lot_step",
    "price_precision",
    "pip_size",
    "assumed_spread",
    "tradeable",
)


class InstrumentConfigError(ValueError):
    """The instrument config is malformed or internally inconsistent."""


class UnsupportedConversionError(ValueError):
    """A price move cannot be converted into the account currency.

    Raised rather than guessed. A wrong cross-rate assumption would mis-size
    every trade on the instrument while looking perfectly plausible.
    """


def _dec(value: Any, field: str, symbol: str) -> Decimal:
    """Convert a YAML scalar to Decimal via str, so 0.1 stays 0.1."""
    try:
        return Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - re-raised with context
        raise InstrumentConfigError(
            f"{symbol}.{field}: cannot read {value!r} as a number"
        ) from exc


@dataclass(frozen=True)
class InstrumentSpec:
    """How one tradeable instrument behaves, for sizing purposes."""

    symbol: str
    kind: str
    base: str
    quote: str
    contract_size: Decimal
    min_lot: Decimal
    lot_step: Decimal
    price_precision: int
    pip_size: Decimal
    assumed_spread: Decimal
    tradeable: bool
    strategies: tuple[str, ...] = ()
    tradeable_note: str = ""

    def quote_to_account(self, account_currency: str, price: Decimal | None) -> Decimal:
        """Factor converting an amount in the quote currency to the account currency.

        Two cases are supported, which covers the whole universe:

        * quote == account (EURUSD, XAUUSD on a USD account) -> 1, no conversion.
        * base == account (USDJPY on a USD account) -> 1/price. The USDJPY price
          *is* JPY per USD, so dividing by it converts JPY back to USD.

        Anything else needs a third instrument's rate and is refused.
        """
        if self.quote == account_currency:
            return Decimal(1)

        if self.base == account_currency:
            if price is None:
                raise UnsupportedConversionError(
                    f"{self.symbol}: quoted in {self.quote} on a {account_currency} "
                    "account, so a current price is needed to convert. Pass price=."
                )
            if price <= 0:
                raise UnsupportedConversionError(
                    f"{self.symbol}: price must be positive, got {price}"
                )
            return Decimal(1) / price

        raise UnsupportedConversionError(
            f"{self.symbol}: cannot convert {self.quote} to {account_currency} without "
            f"a cross rate. Add explicit cross-rate support before trading this."
        )

    def value_per_price_unit(
        self,
        lots: Decimal,
        account_currency: str,
        price: Decimal | None = None,
    ) -> Decimal:
        """Account-currency value of a 1.0 move in price, holding `lots`.

        EURUSD, 1.00 lot: 1.0 * 100_000 * 1 = $100,000 per 1.0 of price, so a
        0.0010 move (10 pips) is $100. XAUUSD, 0.01 lot: 1.0 * 100 * 0.01 = $1.00
        per $1.00 of price move.
        """
        return self.contract_size * lots * self.quote_to_account(account_currency, price)


@dataclass(frozen=True)
class InstrumentUniverse:
    """All configured instruments, plus the account currency they price against."""

    account_currency: str
    instruments: dict[str, InstrumentSpec]

    def __getitem__(self, symbol: str) -> InstrumentSpec:
        try:
            return self.instruments[symbol]
        except KeyError:
            known = ", ".join(sorted(self.instruments))
            raise KeyError(f"unknown instrument {symbol!r}; configured: {known}") from None

    def tradeable(self) -> list[InstrumentSpec]:
        """Instruments that may produce live signals, in config order."""
        return [s for s in self.instruments.values() if s.tradeable]

    def for_strategy(self, strategy: str) -> list[InstrumentSpec]:
        """Tradeable instruments this strategy is allowed to signal on."""
        return [s for s in self.tradeable() if strategy in s.strategies]


def load_instruments(path: str | Path) -> InstrumentUniverse:
    """Read and validate the instrument config."""
    path = Path(path)
    raw = yaml.safe_load(path.read_text())

    if not isinstance(raw, dict) or "instruments" not in raw:
        raise InstrumentConfigError(f"{path}: expected a mapping with an 'instruments' key")

    account_currency = raw.get("account_currency")
    if not account_currency:
        raise InstrumentConfigError(f"{path}: 'account_currency' is required")

    specs: dict[str, InstrumentSpec] = {}
    for symbol, body in raw["instruments"].items():
        if not isinstance(body, dict):
            raise InstrumentConfigError(f"{symbol}: expected a mapping, got {type(body).__name__}")

        missing = [f for f in _REQUIRED if f not in body]
        if missing:
            raise InstrumentConfigError(f"{symbol}: missing required field(s): {', '.join(missing)}")

        spec = InstrumentSpec(
            symbol=symbol,
            kind=body["kind"],
            base=body["base"],
            quote=body["quote"],
            contract_size=_dec(body["contract_size"], "contract_size", symbol),
            min_lot=_dec(body["min_lot"], "min_lot", symbol),
            lot_step=_dec(body["lot_step"], "lot_step", symbol),
            price_precision=int(body["price_precision"]),
            pip_size=_dec(body["pip_size"], "pip_size", symbol),
            assumed_spread=_dec(body["assumed_spread"], "assumed_spread", symbol),
            tradeable=bool(body["tradeable"]),
            strategies=tuple(body.get("strategies") or ()),
            tradeable_note=str(body.get("tradeable_note", "")).strip(),
        )
        _validate(spec)
        specs[symbol] = spec

    if not specs:
        raise InstrumentConfigError(f"{path}: no instruments configured")

    return InstrumentUniverse(account_currency=account_currency, instruments=specs)


def _validate(spec: InstrumentSpec) -> None:
    """Catch the config mistakes that would otherwise mis-size trades in silence."""
    for field in ("contract_size", "min_lot", "lot_step", "pip_size"):
        if getattr(spec, field) <= 0:
            raise InstrumentConfigError(f"{spec.symbol}.{field} must be positive")

    if spec.assumed_spread < 0:
        raise InstrumentConfigError(f"{spec.symbol}.assumed_spread must not be negative")

    if spec.min_lot % spec.lot_step != 0:
        raise InstrumentConfigError(
            f"{spec.symbol}: min_lot {spec.min_lot} is not a multiple of "
            f"lot_step {spec.lot_step}, so the minimum position is unreachable"
        )

    if spec.base == spec.quote:
        raise InstrumentConfigError(f"{spec.symbol}: base and quote are both {spec.base}")

    if spec.tradeable and not spec.strategies:
        raise InstrumentConfigError(
            f"{spec.symbol}: marked tradeable but no strategies are assigned, so it "
            "would never signal. Either assign strategies or set tradeable: false."
        )

    if not spec.tradeable and spec.strategies:
        raise InstrumentConfigError(
            f"{spec.symbol}: not tradeable but has strategies {list(spec.strategies)} "
            "assigned. Remove them so the intent is unambiguous."
        )
