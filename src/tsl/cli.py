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
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .data.bars import resample_bars, ticks_to_bars
from .data.dukascopy import SourceSpec
from .data.quality import check_bars
from .data.store import BarStore, DownloadManifest
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
    from .data.dukascopy import DownloadError, iter_daily_ticks

    universe, _ = _load(args.config)
    spec = universe[args.symbol]
    source = _raw_source(args.config, args.symbol)
    start, end = _parse_day(args.start), _parse_day(args.end)
    if end <= start:
        raise SystemExit("end date must be after start date")

    store = BarStore(args.data)
    manifest = DownloadManifest(args.data)
    already = manifest.completed_days(args.symbol) if not args.restart else set()

    total_days = (end - start).days
    print(f"Downloading {spec.symbol}, {start:%Y-%m-%d} to {end:%Y-%m-%d} ({total_days} days).")
    if already:
        pending = sum(
            1 for i in range(total_days) if (start + timedelta(days=i)).date() not in already
        )
        print(f"{len(already)} day(s) already downloaded and will be skipped; {pending} to fetch.")
    print("Dukascopy is free and throttles heavy use, so this is paced deliberately.")
    print("If it stops, just run the same command again - finished days are not re-fetched.\n")

    def progress(hour, count):
        if hour.hour == 0:
            print(f"  {hour:%Y-%m-%d}  ", end="", flush=True)

    def retry(hour, attempt, delay, why):
        print(f"\n    {why} at {hour:%H}h, waiting {delay:.0f}s (attempt {attempt})",
              end="", flush=True)

    stored_days = 0
    stored_bars = 0
    try:
        for day, ticks in iter_daily_ticks(
            source, start, end,
            pause=args.pause, skip_days=already,
            on_progress=progress, on_retry=retry,
        ):
            if ticks.empty:
                # Weekend or holiday. Record it so a resume does not ask again.
                manifest.mark_complete(args.symbol, day.date())
                print("no ticks (market closed)", flush=True)
                continue

            bars = ticks_to_bars(ticks, BASE_TIMEFRAME)
            store.write(args.symbol, BASE_TIMEFRAME, bars)
            for timeframe in ("15min", "1h", "4h"):
                store.write(args.symbol, timeframe, resample_bars(bars, timeframe))

            manifest.mark_complete(args.symbol, day.date())
            stored_days += 1
            stored_bars += len(bars)
            print(f"{len(ticks):>9,} ticks -> {len(bars):>5,} bars  (saved)", flush=True)

    except DownloadError as exc:
        # Not a traceback. The work so far is already on disk.
        print(f"\n\nDownload stopped: {exc}\n")
        print(f"Saved before stopping: {stored_days} day(s), {stored_bars:,} {BASE_TIMEFRAME} bars.")
        print("Nothing is lost. Re-run the same command to continue where it left off.")
        return 3
    except KeyboardInterrupt:
        print(f"\n\nInterrupted. Saved {stored_days} day(s) - re-run to continue.")
        return 130

    if stored_days == 0:
        print("\nNothing new downloaded.")
        if already:
            print("Every day in that range was already stored. Use --restart to force a re-download.")
            return 0
        print("If the whole range was a weekend that is expected; otherwise check the")
        print("symbol name in the dukascopy block of config/instruments.yaml.")
        return 1

    print(f"\nStored {stored_bars:,} {BASE_TIMEFRAME} bars across {stored_days} day(s).")
    report = check_bars(store.read(args.symbol, BASE_TIMEFRAME), args.symbol, BASE_TIMEFRAME)
    print("\n" + str(report))
    return 0 if report.ok else 2


def cmd_peek(args) -> int:
    """Show real stored prices, so a human can confirm they are actually right.

    The plausible-range check on import only proves the numbers are not absurd.
    A scale error of 2x would pass it. Nothing catches that except somebody who
    knows roughly what the instrument was worth looking at the figures.
    """
    universe, _ = _load(args.config)
    spec = universe[args.symbol]
    bars = BarStore(args.data).read(args.symbol, args.timeframe)

    if bars.empty:
        print(f"No {args.timeframe} bars stored for {args.symbol}.")
        print(f"Download some first:  python -m tsl download {args.symbol} 2024-01-08 2024-01-11")
        return 1

    dp = spec.price_precision
    print(f"{args.symbol} {args.timeframe} - {len(bars):,} bars, "
          f"{bars.index[0]:%Y-%m-%d %H:%M} to {bars.index[-1]:%Y-%m-%d %H:%M} UTC\n")

    def show(rows, title):
        print(title)
        for ts, row in rows.iterrows():
            print(f"  {ts:%Y-%m-%d %H:%M}  O {row['open']:>10.{dp}f}  H {row['high']:>10.{dp}f}  "
                  f"L {row['low']:>10.{dp}f}  C {row['close']:>10.{dp}f}   "
                  f"spread {row['spread_mean']:.{dp}f}")

    show(bars.head(3), "First bars:")
    if len(bars) > 6:
        print("  ...")
        show(bars.tail(3), "")

    close = bars["close"]
    print(f"\nSummary:")
    print(f"  close    min {close.min():.{dp}f}   max {close.max():.{dp}f}   mean {close.mean():.{dp}f}")
    print(f"  spread   median {bars['spread_mean'].median():.{dp}f}   "
          f"max {bars['spread_max'].max():.{dp}f}   "
          f"(assumed in config: {float(spec.assumed_spread):.{dp}f})")

    days = bars.index.normalize().nunique()
    print(f"  coverage {days} day(s), about {len(bars) // max(days, 1):,} bars per day")

    print("\n" + "-" * 70)
    print("SANITY CHECK - do these prices look right to you?")
    print(f"Pull up a {args.symbol} chart for {bars.index[0]:%B %Y} and compare.")
    print("A scale error is off by a factor of 10, 100 or 1000, so it is obvious")
    print("once you look. If the numbers are wrong, the point_scale for")
    print(f"{args.symbol} in config/instruments.yaml needs changing - tell me by how much.")

    if bars["spread_mean"].median() > float(spec.assumed_spread) * 5:
        print("\nNOTE: the real spread is far wider than the config assumes. Worth")
        print("      raising assumed_spread before backtesting, or costs will be")
        print("      understated and results too optimistic.")
    return 0


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
    p.add_argument("--pause", type=float, default=0.5,
                   help="seconds between requests (default 0.5; raise it if throttled)")
    p.add_argument("--restart", action="store_true",
                   help="re-download days already recorded as complete")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("peek", help="show stored prices so you can verify they are right")
    p.add_argument("symbol")
    p.add_argument("timeframe", nargs="?", default="15min")
    p.set_defaults(func=cmd_peek)

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
