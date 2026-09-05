"""Position sizing.

The worked examples here are lifted straight from docs/POSITION-SIZING-REALITY-CHECK.md.
If the code and that document ever disagree, these tests fail - which is the point.
"""

from decimal import Decimal

import pytest

from tsl.instruments import load_instruments
from tsl.risk import RejectReason, load_risk_config, size_position

D = Decimal


@pytest.fixture(scope="module")
def universe():
    return load_instruments("config/instruments.yaml")


@pytest.fixture(scope="module")
def risk():
    return load_risk_config("config/risk.yaml")


def test_budget_is_ten_dollars(risk):
    assert risk.equity == D("1000.00")
    assert risk.risk_pct == D("1.0")
    assert risk.risk_budget == D("10.00")


def test_configured_portfolio_limits(risk):
    """The limits confirmed in DECISIONS.md D16."""
    assert risk.max_open_positions == 3
    assert risk.max_positions_per_currency == 2
    assert risk.daily_stop_r == D("-3.0")


# --- the worked examples -----------------------------------------------------


def test_eurusd_twenty_pip_stop_sizes_to_five_hundredths(universe, risk):
    """$10 risk / (0.0020 * 100,000) = 0.05 lots."""
    result = size_position(
        universe["EURUSD"], risk, entry=D("1.10000"), stop=D("1.09800")
    )
    assert result.accepted
    assert result.lots == D("0.05")
    assert result.risk_amount == D("10.00")


def test_gbpusd_twentyfive_pip_stop(universe, risk):
    """$10 / (0.0025 * 100,000) = 0.04 lots."""
    result = size_position(
        universe["GBPUSD"], risk, entry=D("1.27000"), stop=D("1.26750")
    )
    assert result.accepted
    assert result.lots == D("0.04")


def test_usdjpy_sizes_through_its_own_price(universe, risk):
    """25 pips at 150.00: risk per lot is 0.25 * 100,000/150 = $166.67, so 0.06 lots."""
    result = size_position(
        universe["USDJPY"], risk, entry=D("150.000"), stop=D("149.750"), price=D("150")
    )
    assert result.accepted
    assert result.lots == D("0.06")
    assert result.risk_amount <= risk.risk_budget


def test_gold_calm_day_fits_but_only_just(universe, risk):
    """A $6 stop wants 0.0167 lots; rounding down to 0.01 risks $6 - under budget,
    but it means only 60% of the intended risk is taken. Coarse, not broken."""
    result = size_position(universe["XAUUSD"], risk, entry=D("2412.50"), stop=D("2406.50"))
    assert result.accepted
    assert result.lots == D("0.01")
    assert result.risk_amount == D("6.00")


def test_gold_volatile_day_is_rejected_not_oversized(universe, risk):
    """THE GUARD RAIL. A $12 stop wants 0.0083 lots. The 0.01 minimum would risk
    $12 - 120% of budget - so the trade is skipped, not rounded up."""
    result = size_position(universe["XAUUSD"], risk, entry=D("2412.50"), stop=D("2400.50"))
    assert result.rejected
    assert result.reason is RejectReason.BELOW_MINIMUM_LOT
    assert result.lots == D("0")
    assert "12.00" in result.detail
    assert "SKIPPED" in result.describe()


def test_silver_is_refused_as_not_tradeable(universe, risk):
    result = size_position(universe["XAGUSD"], risk, entry=D("30.000"), stop=D("29.700"))
    assert result.rejected
    assert result.reason is RejectReason.NOT_TRADEABLE


def test_silver_would_be_rejected_on_size_even_if_enabled(universe, risk):
    """Independent of the tradeable flag: a $0.30 stop needs 0.0067 lots, and the
    0.01 minimum (50oz) risks $15. Enabling silver would not make it viable."""
    enabled = universe["XAGUSD"].__class__(
        **{**universe["XAGUSD"].__dict__, "tradeable": True}
    )
    result = size_position(enabled, risk, entry=D("30.000"), stop=D("29.700"))
    assert result.rejected
    assert result.reason is RejectReason.BELOW_MINIMUM_LOT
    assert "15.00" in result.detail


# --- properties that must hold for every trade -------------------------------


@pytest.mark.parametrize(
    "symbol,entry,stop,price",
    [
        ("EURUSD", "1.10000", "1.09800", None),
        ("EURUSD", "1.10000", "1.09650", None),
        ("GBPUSD", "1.27000", "1.26600", None),
        ("AUDUSD", "0.65000", "0.64800", None),
        ("USDJPY", "150.000", "149.700", "150"),
        ("XAUUSD", "2412.50", "2404.50", None),
        ("XAUUSD", "2412.50", "2409.00", None),
    ],
)
def test_accepted_trades_never_exceed_the_budget(universe, risk, symbol, entry, stop, price):
    """The invariant the whole module exists to guarantee."""
    result = size_position(
        universe[symbol], risk, D(entry), D(stop), D(price) if price else None
    )
    if result.accepted:
        assert result.risk_amount <= risk.risk_budget, result.describe()


@pytest.mark.parametrize("symbol", ["EURUSD", "GBPUSD", "AUDUSD", "XAUUSD"])
def test_direction_does_not_change_size(universe, risk, symbol):
    """A long and a short with the same stop distance must size identically."""
    spec = universe[symbol]
    entry = D("100.00000")
    long = size_position(spec, risk, entry=entry, stop=entry - D("0.50000"))
    short = size_position(spec, risk, entry=entry, stop=entry + D("0.50000"))
    assert long.lots == short.lots


def test_lots_are_always_a_whole_number_of_steps(universe, risk):
    spec = universe["EURUSD"]
    for stop_pips in range(5, 200, 3):
        stop = D("1.10000") - D(stop_pips) * D("0.0001")
        result = size_position(spec, risk, entry=D("1.10000"), stop=stop)
        if result.accepted:
            assert result.lots % spec.lot_step == 0, f"{result.lots} at {stop_pips} pips"


def test_wider_stop_never_gives_a_bigger_position(universe, risk):
    """Monotonicity: more risk per lot must mean no more lots."""
    spec = universe["EURUSD"]
    previous = None
    for stop_pips in range(10, 120, 5):
        stop = D("1.10000") - D(stop_pips) * D("0.0001")
        result = size_position(spec, risk, entry=D("1.10000"), stop=stop)
        if previous is not None:
            assert result.lots <= previous
        previous = result.lots


def test_zero_stop_distance_is_rejected(universe, risk):
    result = size_position(universe["EURUSD"], risk, entry=D("1.10000"), stop=D("1.10000"))
    assert result.rejected
    assert result.reason is RejectReason.ZERO_STOP_DISTANCE


# --- the overshoot escape hatch ----------------------------------------------


def test_overshoot_allowance_can_admit_a_marginal_trade(universe, risk):
    """With a 25% overshoot allowed, the $12 gold trade becomes permissible.
    Default config sets 1.0, so this is opt-in rather than accidental."""
    lenient = risk.__class__(**{**risk.__dict__, "max_budget_overshoot": D("1.25")})
    result = size_position(universe["XAUUSD"], lenient, entry=D("2412.50"), stop=D("2400.50"))
    assert result.accepted
    assert result.lots == D("0.01")
    assert result.risk_amount == D("12.00")
    assert result.risk_amount > lenient.risk_budget


def test_default_config_allows_no_overshoot(risk):
    assert risk.max_budget_overshoot == D("1.0")


# --- regression --------------------------------------------------------------


def test_repeating_division_does_not_lose_a_lot_step(universe, risk):
    """Regression: USDJPY divides by its own price, so risk-per-lot repeats.

    The exact answer here is 0.06 lots, but Decimal returns
    0.05999999999999999999999999999. Flooring that to the 0.01 step without
    snapping first gave 0.05 - a sixth of the position lost to rounding dust.
    """
    result = size_position(
        universe["USDJPY"], risk, entry=D("150.000"), stop=D("149.750"), price=D("150")
    )
    assert result.lots == D("0.06")
    assert result.risk_amount == D("10.00")


@pytest.mark.parametrize("price", ["3", "7", "150", "151.37", "99.99"])
def test_snapping_never_breaches_the_budget(universe, risk, price):
    """The epsilon snap rounds up, so prove it cannot push risk over budget."""
    spec = universe["USDJPY"]
    for stop_pips in range(5, 80, 1):
        stop = D(price) - D(stop_pips) * spec.pip_size
        result = size_position(spec, risk, entry=D(price), stop=stop, price=D(price))
        if result.accepted:
            assert result.risk_amount <= risk.risk_budget, result.describe()
