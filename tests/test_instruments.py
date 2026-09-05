"""Instrument config loading and the price-move-to-money conversion."""

from decimal import Decimal

import pytest

from tsl.instruments import (
    InstrumentConfigError,
    UnsupportedConversionError,
    load_instruments,
)

CONFIG = "config/instruments.yaml"


@pytest.fixture(scope="module")
def universe():
    return load_instruments(CONFIG)


def test_universe_loads_expected_instruments(universe):
    assert set(universe.instruments) == {
        "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "XAUUSD", "XAGUSD"
    }
    assert universe.account_currency == "USD"


def test_silver_is_not_tradeable(universe):
    """Per POSITION-SIZING-REALITY-CHECK.md: silver stays in research only."""
    assert universe["XAGUSD"].tradeable is False
    assert universe["XAGUSD"].strategies == ()
    assert [s.symbol for s in universe.tradeable()] == [
        "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "XAUUSD"
    ]


def test_breakout_excludes_asian_session_instruments(universe):
    """STRATEGY-SPEC.md: AUDUSD and USDJPY trade actively during Asian hours, so
    their overnight range is a session rather than a coil."""
    breakout = {s.symbol for s in universe.for_strategy("london_breakout")}
    assert breakout == {"EURUSD", "GBPUSD", "XAUUSD"}
    assert "USDJPY" not in breakout
    assert "AUDUSD" not in breakout


def test_pullback_covers_every_tradeable_instrument(universe):
    pullback = {s.symbol for s in universe.for_strategy("trend_pullback")}
    assert pullback == {"EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "XAUUSD"}


def test_unknown_instrument_names_the_alternatives(universe):
    with pytest.raises(KeyError, match="EURUSD"):
        universe["NZDUSD"]


# --- the conversion, which every position size depends on --------------------


def test_eurusd_ten_pip_move_on_one_lot_is_100_dollars(universe):
    spec = universe["EURUSD"]
    per_unit = spec.value_per_price_unit(Decimal("1.00"), "USD")
    assert per_unit * Decimal("0.0010") == Decimal("100.0000")


def test_gold_one_dollar_move_on_min_lot_is_one_dollar(universe):
    """0.01 lot of gold is 1 troy ounce, so $1 of price is $1 of P&L."""
    spec = universe["XAUUSD"]
    per_unit = spec.value_per_price_unit(Decimal("0.01"), "USD")
    assert per_unit * Decimal("1.00") == Decimal("1.0000")


def test_silver_min_lot_is_fifty_ounces(universe):
    """0.01 lot of silver is 50oz - the reason it cannot be traded at $10 risk."""
    spec = universe["XAGUSD"]
    per_unit = spec.value_per_price_unit(Decimal("0.01"), "USD")
    assert per_unit == Decimal("50.00")


def test_usdjpy_converts_through_its_own_price(universe):
    """A JPY-quoted instrument on a USD account: 1 pip at 1 lot is ~$6.67 at 150."""
    spec = universe["USDJPY"]
    per_unit = spec.value_per_price_unit(Decimal("1.00"), "USD", price=Decimal("150"))
    one_pip = per_unit * Decimal("0.01")
    assert one_pip == pytest.approx(Decimal("6.6667"), abs=Decimal("0.001"))


def test_usdjpy_without_a_price_refuses_rather_than_guesses(universe):
    with pytest.raises(UnsupportedConversionError, match="current price is needed"):
        universe["USDJPY"].value_per_price_unit(Decimal("1.00"), "USD")


def test_conversion_needing_a_cross_rate_is_refused(universe):
    """EURUSD on a JPY account would need a third rate. Refuse, don't guess."""
    with pytest.raises(UnsupportedConversionError, match="cross rate"):
        universe["EURUSD"].value_per_price_unit(Decimal("1.00"), "JPY")


# --- config validation -------------------------------------------------------


def _write(tmp_path, body: str):
    p = tmp_path / "instruments.yaml"
    p.write_text("account_currency: USD\ninstruments:\n" + body)
    return p


BASE = """  TESTFX:
    kind: fx
    base: EUR
    quote: USD
    contract_size: 100000
    min_lot: 0.01
    lot_step: 0.01
    price_precision: 5
    pip_size: 0.0001
    assumed_spread: 0.0001
    tradeable: true
    strategies: [trend_pullback]
"""


def test_valid_minimal_config_loads(tmp_path):
    assert load_instruments(_write(tmp_path, BASE))["TESTFX"].kind == "fx"


def test_missing_field_is_rejected(tmp_path):
    body = BASE.replace("    contract_size: 100000\n", "")
    with pytest.raises(InstrumentConfigError, match="contract_size"):
        load_instruments(_write(tmp_path, body))


def test_min_lot_not_a_multiple_of_step_is_rejected(tmp_path):
    body = BASE.replace("    min_lot: 0.01\n", "    min_lot: 0.015\n")
    with pytest.raises(InstrumentConfigError, match="not a multiple"):
        load_instruments(_write(tmp_path, body))


def test_tradeable_with_no_strategies_is_rejected(tmp_path):
    """Would never signal - almost certainly a mistake, so say so at load time."""
    body = BASE.replace("    strategies: [trend_pullback]\n", "    strategies: []\n")
    with pytest.raises(InstrumentConfigError, match="no strategies"):
        load_instruments(_write(tmp_path, body))


def test_non_tradeable_with_strategies_is_rejected(tmp_path):
    body = BASE.replace("    tradeable: true\n", "    tradeable: false\n")
    with pytest.raises(InstrumentConfigError, match="not tradeable but has strategies"):
        load_instruments(_write(tmp_path, body))


def test_negative_contract_size_is_rejected(tmp_path):
    body = BASE.replace("    contract_size: 100000\n", "    contract_size: -100000\n")
    with pytest.raises(InstrumentConfigError, match="must be positive"):
        load_instruments(_write(tmp_path, body))
