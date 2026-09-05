"""Command line interface.

Written for someone who does not read Python. Every command prints what it is
doing, what it found, and what to do next when something goes wrong.

    python -m tsl instruments                    what is configured, and why
    python -m tsl size EURUSD 1.10000 1.09800    what would this trade cost me?
    python -m tsl download EURUSD 2024-01-01 2024-02-01
    python -m tsl check EURUSD 15min             is the stored data trustworthy?
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .data.bars import resample_bars, ticks_to_bars
from .data.dukascopy import SourceSpec
from .data.quality import check_bars
from .data.store import BarStore
from .instruments import load_instruments
from .risk import load_risk_config, size_position

DEFAULT_CONFIG = Path("config")
DEFAULT_DATA = Path("data")
BASE_TIMEFRAME = "1min"


def _load(config_dir: Path):
    return (
        load_instruments(config_dir / "instruments.yaml"),
        load_risk_config(config_dir / "risk.yaml"),
    )


def _raw_source(config_dir: Path, symbol: str) -> SourceSpec:
    import yaml

    raw = yaml.safe_load((config_dir / "instruments.yaml").read_text())
    body = raw["instruments"].get(symbol)
    if body is None:
        raise SystemExit(f"unknown instrument {symbol!r}")
    if "dukascopy" not in body:
        raise SystemExit(f"{symbol} has no 'dukascopy' block in instruments.yaml")
    return SourceSpec.from_config(body["dukascopy"])


def _parse_day(text: str) -> datetime:
    try:
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise SystemExit(f"date {text!r} must look like 2024-01-31") from None


# --- commands ----------------------------------------------------------------


def cmd_instruments(args) -> int:
    universe, risk = _load(args.config)
    print(f"Account: {risk.equity} {risk.account_currency}, "
          f"risking {risk.risk_pct}% = {risk.risk_budget} per trade\n")

    for spec in universe.instruments.values():
        status = "tradeable" if spec.tradeable else "RESEARCH ONLY"
        print(f"{spec.symbol:8} {status:14} contract {spec.contract_size:>8} "
              f"min lot {spec.min_lot}  strategies: {', '.join(spec.strategies) or '-'}")
        if spec.tradeable_note:
            print(f"{'':8} note: {spec.tradeable_note}")
    return 0


def cmd_size(args) -> int:
    universe, risk = _load(args.config)
    try:
        entry, stop = Decimal(args.entry), Decimal(args.stop)
        price = Decimal(args.price) if args.price else entry
    except InvalidOperation:
        raise SystemExit("entry, stop and price must be numbers") from None

    result = size_position(universe[args.symbol], risk, entry, stop, price)
    print(result.describe())

    if result.accepted:
        # Show the exit ladder so the whole trade is visible in one place.
        risk_distance = abs(entry - stop)
        direction = 1 if entry > stop else -1
        print(f"  stop distance {risk_distance} (1R)")
        for multiple in (Decimal(2), Decimal(3)):
            print(f"  {multiple}R target: {entry + direction * multiple * risk_distance}")
    return 0 if result.accepted else 1


def cmd_download(args) -> int:
    from .data.dukascopy import download_ticks

    universe, _ = _load(args.config)
    spec = universe[args.symbol]
    source = _raw_source(args.config, args.symbol)
    start, end = _parse_day(args.start), _parse_day(args.end)
    if end <= start:
        raise SystemExit("end date must be after start date")

    hours = int((end - start).total_seconds() // 3600)
    print(f"Downloading {spec.symbol} ticks, {start:%Y-%m-%d} to {end:%Y-%m-%d} ({hours} hours).")
    print("Dukascopy is a free service - this is deliberately paced and will take a while.\n")

    seen = {"hours": 0, "ticks": 0}

    def progress(hour, count):
        seen["hours"] += 1
        seen["ticks"] += count
        if seen["hours"] % 24 == 0 or count == 0:
            pct = 100 * seen["hours"] / max(hours, 1)
            print(f"  {hour:%Y-%m-%d %H}h  {seen['ticks']:>9,} ticks  ({pct:.0f}%)", flush=True)

    ticks = download_ticks(args.symbol, source, start, end, on_progress=progress)

    if ticks.empty:
        print("\nNo ticks returned. If the whole range was a weekend that is expected;")
        print("otherwise check the symbol name in the dukascopy block of instruments.yaml.")
        return 1

    bars = ticks_to_bars(ticks, BASE_TIMEFRAME)
    store = BarStore(args.data)
    written = store.write(args.symbol, BASE_TIMEFRAME, bars)
    print(f"\nStored {len(bars):,} {BASE_TIMEFRAME} bars from {len(ticks):,} ticks "
          f"into {len(written)} file(s).")

    for timeframe in ("15min", "1h", "4h"):
        rolled = resample_bars(bars, timeframe)
        store.write(args.symbol, timeframe, rolled)
        print(f"  rolled up to {timeframe}: {len(rolled):,} bars")

    report = check_bars(bars, args.symbol, BASE_TIMEFRAME)
    print("\n" + str(report))
    return 0 if report.ok else 2


def cmd_check(args) -> int:
    store = BarStore(args.data)
    bars = store.read(args.symbol, args.timeframe)
    report = check_bars(bars, args.symbol, args.timeframe)
    print(report)

    if not bars.empty:
        print(f"\n  coverage: {bars.index[0]:%Y-%m-%d %H:%M} to {bars.index[-1]:%Y-%m-%d %H:%M} UTC")
    return 0 if report.ok else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tsl", description=__doc__.split("\n")[0])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="config directory")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA, help="data directory")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("instruments", help="show configured instruments").set_defaults(func=cmd_instruments)

    p = sub.add_parser("size", help="size a hypothetical trade")
    p.add_argument("symbol")
    p.add_argument("entry")
    p.add_argument("stop")
    p.add_argument("--price", help="current rate, needed for JPY-quoted instruments")
    p.set_defaults(func=cmd_size)

    p = sub.add_parser("download", help="download tick data and store it as bars")
    p.add_argument("symbol")
    p.add_argument("start", help="YYYY-MM-DD, inclusive")
    p.add_argument("end", help="YYYY-MM-DD, exclusive")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("check", help="run quality checks over stored bars")
    p.add_argument("symbol")
    p.add_argument("timeframe", nargs="?", default="15min")
    p.set_defaults(func=cmd_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
