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
