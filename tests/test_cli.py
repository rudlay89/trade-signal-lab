"""CLI behaviour. These are the commands a non-programmer actually types, so the
output being readable and the exit codes being meaningful are part of the contract."""

import pytest

from tsl.cli import main


def test_instruments_lists_everything_and_explains_silver(capsys):
    assert main(["instruments"]) == 0
    out = capsys.readouterr().out
    assert "EURUSD" in out and "XAGUSD" in out
    assert "RESEARCH ONLY" in out
    assert "1000.0 USD" in out and "10.00 per trade" in out


def test_size_prints_the_full_trade(capsys):
    assert main(["size", "EURUSD", "1.10000", "1.09800"]) == 0
    out = capsys.readouterr().out
    assert "0.05 lots" in out
    assert "2R target: 1.10400" in out
    assert "3R target: 1.10600" in out


def test_size_shows_short_targets_below_entry(capsys):
    main(["size", "EURUSD", "1.10000", "1.10200"])
    out = capsys.readouterr().out
    assert "2R target: 1.09600" in out


def test_rejected_trade_exits_nonzero_and_says_why(capsys):
    """Exit code matters: it lets the guard rail be noticed in a script."""
    assert main(["size", "XAUUSD", "2412.50", "2400.50"]) == 1
    assert "SKIPPED" in capsys.readouterr().out


def test_unknown_instrument_is_a_clear_error():
    with pytest.raises(KeyError, match="configured"):
        main(["size", "NZDUSD", "0.60000", "0.59800"])


def test_check_on_an_empty_store_reports_rather_than_crashing(capsys, tmp_path):
    assert main(["--data", str(tmp_path), "check", "EURUSD", "15min"]) == 2
    assert "no bars" in capsys.readouterr().out


def test_download_rejects_a_backwards_date_range():
    with pytest.raises(SystemExit, match="after start"):
        main(["download", "EURUSD", "2024-02-01", "2024-01-01"])


def test_download_rejects_a_malformed_date():
    with pytest.raises(SystemExit, match="2024-01-31"):
        main(["download", "EURUSD", "01/01/2024", "2024-01-01"])


# --- peek: human verification of stored prices -------------------------------


def _seed_store(tmp_path, symbol="XAUUSD", price=2030.0, spread=0.30):
    """Store a day of bars that look like the instrument really trades."""
    from datetime import datetime, timezone

    from tsl.data.bars import ticks_to_bars
    from tsl.data.store import BarStore
    from tsl.data.synthetic import synthetic_ticks

    ticks = synthetic_ticks(
        datetime(2024, 1, 8, tzinfo=timezone.utc), hours=6, ticks_per_hour=600,
        start_price=price, spread=spread,
        # Scale the step size to the price, or a walk tuned for gold sends
        # EURUSD to implausible levels within a few hundred ticks.
        volatility=price * 2e-5, seed=1,
    )
    bars = ticks_to_bars(ticks, "15min")
    BarStore(tmp_path).write(symbol, "15min", bars)
    return bars


def test_peek_shows_real_prices_and_asks_for_a_human_check(capsys, tmp_path):
    _seed_store(tmp_path)
    assert main(["--data", str(tmp_path), "peek", "XAUUSD", "15min"]) == 0

    out = capsys.readouterr().out
    assert "2024-01-08" in out
    assert "SANITY CHECK" in out
    assert "point_scale" in out
    assert "203" in out          # the price actually appears, at the right magnitude
    assert "bars per day" in out


def test_peek_warns_when_the_real_spread_dwarfs_the_assumption(capsys, tmp_path):
    """Costs being understated is the quiet way a backtest flatters itself."""
    _seed_store(tmp_path, spread=8.0)   # config assumes 0.35 for gold
    main(["--data", str(tmp_path), "peek", "XAUUSD", "15min"])
    out = capsys.readouterr().out
    assert "far wider than the config assumes" in out
    assert "too optimistic" in out


def test_peek_is_quiet_when_the_spread_matches(capsys, tmp_path):
    _seed_store(tmp_path, spread=0.30)
    main(["--data", str(tmp_path), "peek", "XAUUSD", "15min"])
    assert "far wider" not in capsys.readouterr().out


def test_peek_with_nothing_stored_says_what_to_run(capsys, tmp_path):
    assert main(["--data", str(tmp_path), "peek", "EURUSD", "15min"]) == 1
    assert "download EURUSD" in capsys.readouterr().out


def test_peek_uses_the_instruments_price_precision(capsys, tmp_path):
    """EURUSD needs 5 decimals; gold at 5 decimals would be unreadable."""
    _seed_store(tmp_path, symbol="EURUSD", price=1.09400, spread=0.00012)
    main(["--data", str(tmp_path), "peek", "EURUSD", "15min"])
    assert "1.09" in capsys.readouterr().out
