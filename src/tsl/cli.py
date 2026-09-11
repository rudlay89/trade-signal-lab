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
import time
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
    """Download history and store it as bars.

    Two sources. Candle files cost two requests per instrument-day; tick files
    cost twenty-four for the same 1-minute bars. Candles are the default because
    throttling, not bandwidth, is what makes a multi-year download slow. Ticks
    remain available for any period needing sub-minute detail.
    """
    import requests

    from .data.candles import download_day_candles
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
    per_day = 24 if args.source == "ticks" else 2
    print(f"Downloading {spec.symbol}, {start:%Y-%m-%d} to {end:%Y-%m-%d} "
          f"({total_days} days) from {args.source}.")
    if already:
        pending = sum(
            1 for i in range(total_days) if (start + timedelta(days=i)).date() not in already
        )
        print(f"{len(already)} day(s) already downloaded and will be skipped; {pending} to fetch.")
        total_days = pending
    print(f"About {total_days * per_day:,} requests at {args.pause}s apart.")
    print("If it stops, run the same command again - finished days are not re-fetched.\n")

    stored_days = 0
    stored_bars = 0
    reported_layout = False

    def retry(when, attempt, delay, why):
        print(f"\n    {why} at {when:%Y-%m-%d %H}h, waiting {delay:.0f}s (attempt {attempt})",
              end="", flush=True)

    def persist(day, bars) -> None:
        nonlocal stored_days, stored_bars
        store.write(args.symbol, BASE_TIMEFRAME, bars)
        for timeframe in ("15min", "1h", "4h"):
            store.write(args.symbol, timeframe, resample_bars(bars, timeframe))
        manifest.mark_complete(args.symbol, day)
        stored_days += 1
        stored_bars += len(bars)

    try:
        if args.source == "candles":
            from .data.candles import days_between

            session = requests.Session()
            try:
                for day in days_between(start, end):
                    if day.date() in already:
                        continue
                    print(f"  {day:%Y-%m-%d}  ", end="", flush=True)
                    bars = download_day_candles(session, source, day, on_retry=retry)

                    if bars.empty:
                        manifest.mark_complete(args.symbol, day.date())
                        print("no candles (market closed)", flush=True)
                    else:
                        if not reported_layout and bars.attrs.get("field_order"):
                            print(f"[{'/'.join(bars.attrs['field_order'])}] ", end="")
                            reported_layout = True
                        persist(day.date(), bars)
                        print(f"{len(bars):>5,} bars  (saved)", flush=True)

                    if args.pause:
                        time.sleep(args.pause)
            finally:
                session.close()
        else:
            for day, ticks in iter_daily_ticks(
                source, start, end, pause=args.pause, skip_days=already, on_retry=retry,
            ):
                print(f"  {day:%Y-%m-%d}  ", end="", flush=True)
                if ticks.empty:
                    manifest.mark_complete(args.symbol, day.date())
                    print("no ticks (market closed)", flush=True)
                    continue
                bars = ticks_to_bars(ticks, BASE_TIMEFRAME)
                persist(day.date(), bars)
                print(f"{len(ticks):>9,} ticks -> {len(bars):>5,} bars  (saved)", flush=True)

    except DownloadError as exc:
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


def cmd_verify_candles(args) -> int:
    """Prove the candle record layout by comparing it against tick-derived bars.

    Tick data is expensive to download and candle data is cheap, but the candle
    record layout is an assumption. This settles it: fetch a day that has already
    been built from raw ticks, build the same day from candles, and compare them
    minute by minute. Two independent paths agreeing is proof; a plausibility
    check is not.
    """
    import requests

    from .data.candles import compare_bar_series, download_day_candles
    from .data.dukascopy import DownloadError

    universe, _ = _load(args.config)
    universe[args.symbol]  # validate the symbol
    source = _raw_source(args.config, args.symbol)
    store = BarStore(args.data)

    stored = store.read(args.symbol, BASE_TIMEFRAME)
    if stored.empty:
        print(f"No tick-derived bars stored for {args.symbol}, so there is nothing to")
        print("compare against. Download a day or two of ticks first:")
        print(f"  python -m tsl download {args.symbol} 2024-01-08 2024-01-10")
        return 1

    days = sorted({ts.date() for ts in stored.index})[: args.days]
    print(f"Verifying the candle format for {args.symbol} against "
          f"{len(days)} day(s) of tick-derived bars.")
    print(f"This costs {len(days) * 2} requests - two per day, versus 24 for ticks.\n")

    session = requests.Session()
    try:
        for day in days:
            when = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
            print(f"  {day}  fetching... ", end="", flush=True)
            try:
                from_candles = download_day_candles(session, source, when)
            except DownloadError as exc:
                print(f"\n\nDownload failed: {exc}")
                return 3

            if from_candles.empty:
                print("no candles returned (market closed?)")
                continue

            order = from_candles.attrs.get("field_order")
            if order:
                print(f"decoded as {'/'.join(order)}")
                print(f"  {'':10}  ", end="")
            if "ambiguous" in from_candles.attrs:
                print("\n    NOTE: more than one field order was self-consistent on this")
                print("          day. The comparison below is what settles it.")
                print(f"  {'':10}  ", end="")

            from_ticks = stored[stored.index.normalize().date == day]
            result = compare_bar_series(from_ticks, from_candles)

            print(f"{result['overlapping']:,} overlapping minutes")
            if result["overlapping"] == 0:
                print(f"    {result['verdict']}")
                continue

            print(f"    worst difference {result['worst_diff']:.6g}, "
                  f"typical spread {result['typical_spread']:.6g}")
            for column in ("open", "high", "low", "close"):
                print(f"      {column:6} max {result[f'{column}_max_diff']:.6g}   "
                      f"median {result[f'{column}_median_diff']:.6g}")
            print(f"    {result['verdict']}")

            if not result["passed"]:
                print("\nThe two sources disagree by more than a spread. Most likely the")
                print("field order assumed in src/tsl/data/candles.py is wrong.")
                print("Do not use candle downloads until this is resolved.")
                return 2
    finally:
        session.close()

    print("\nCandle format verified against independently derived tick data.")
    print("Bulk downloads can use it - roughly twelve times fewer requests than")
    print("tick files for the same 1-minute bars.")
    return 0


def cmd_diagnose(args) -> int:
    """Measure the padding signatures against real stored data.

    Dukascopy candle files pad minutes that never traded. This reports what the
    two candidate signatures - no volume, no movement - actually pick out, so the
    filter rests on evidence rather than an assumption about the feed.
    """
    from .data.padding import padding_report

    bars = BarStore(args.data).read(args.symbol, args.timeframe)
    if bars.empty:
        print(f"No {args.timeframe} bars stored for {args.symbol}.")
        return 1

    report = padding_report(bars)
    print(f"{args.symbol} {args.timeframe} - padding analysis")
    print(f"  {bars.index[0]:%Y-%m-%d} to {bars.index[-1]:%Y-%m-%d} UTC\n")
    print(report)

    print("\n" + "-" * 70)
    if report.weekend:
        print("WEEKEND BARS PRESENT. The market is shut at weekends, so these are")
        print("padding. Any bar on a Saturday is proof the feed pads.")
    if not report.signatures_agree:
        print("\nTHE TWO SIGNATURES DISAGREE.")
        print(f"  'no volume' finds {report.zero_volume:,}, 'no movement' finds {report.flat:,},")
        print(f"  but only {report.both:,} satisfy both.")
        print("  The filter requires both, so it is the conservative choice - it")
        print("  keeps anything ambiguous. Worth a look before trusting it.")
    else:
        print("\nBoth signatures agree, so padded minutes are unambiguous.")

    if report.both:
        print(f"\n{report.both:,} bar(s) would be removed by `tsl repair {args.symbol}`.")
    else:
        print("\nNo padding found. Nothing to repair.")
    return 0


def cmd_repair(args) -> int:
    """Strip padded minutes from already-stored bars, without re-downloading.

    The padding is detectable after the fact because volume is stored, so a year
    of downloading does not have to be repeated to fix it.
    """
    from .data.padding import drop_padding, padding_report

    store = BarStore(args.data)
    timeframes = [args.timeframe] if args.timeframe else [BASE_TIMEFRAME, "15min", "1h", "4h"]

    base = store.read(args.symbol, BASE_TIMEFRAME)
    if base.empty:
        print(f"No bars stored for {args.symbol}.")
        return 1

    before = padding_report(base)
    if before.both == 0:
        print(f"{args.symbol}: no padded bars found. Nothing to do.")
        return 0

    print(f"{args.symbol}: removing {before.both:,} padded minute(s) "
          f"of {before.total:,}, including {before.weekend_both:,} at weekends.")

    if args.dry_run:
        print("\nDry run - nothing written. Drop --dry-run to apply.")
        return 0

    cleaned, dropped = drop_padding(base)
    if cleaned.empty:
        print("\nEvery bar looks like padding. Refusing to empty the store.")
        return 2

    # Rebuild every timeframe from the cleaned base, so the roll-ups cannot
    # disagree with it.
    _rewrite(store, args.symbol, BASE_TIMEFRAME, cleaned)
    for timeframe in ("15min", "1h", "4h"):
        _rewrite(store, args.symbol, timeframe, resample_bars(cleaned, timeframe))

    print(f"\nDone. {len(cleaned):,} bars remain, and the 15min/1h/4h series were")
    print("rebuilt from them so nothing disagrees.")

    report = check_bars(cleaned, args.symbol, BASE_TIMEFRAME)
    print("\n" + str(report))
    return 0 if report.ok else 2


def _rewrite(store: BarStore, symbol: str, timeframe: str, bars) -> None:
    """Replace a stored series outright.

    The store merges on write, so a padded bar would survive a plain write. The
    old files are removed first.
    """
    import shutil

    directory = store.root / "bars" / symbol / timeframe
    if directory.exists():
        shutil.rmtree(directory)
    store.write(symbol, timeframe, bars)


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
    p.add_argument("--source", choices=("candles", "ticks"), default="candles",
                   help="candles: 2 requests/day (default). ticks: 24/day, sub-minute detail")
    p.add_argument("--pause", type=float, default=0.5,
                   help="seconds between requests (default 0.5; raise it if throttled)")
    p.add_argument("--restart", action="store_true",
                   help="re-download days already recorded as complete")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("peek", help="show stored prices so you can verify they are right")
    p.add_argument("symbol")
    p.add_argument("timeframe", nargs="?", default="15min")
    p.set_defaults(func=cmd_peek)

    p = sub.add_parser("verify-candles",
                       help="prove the candle format against already-downloaded tick data")
    p.add_argument("symbol")
    p.add_argument("--days", type=int, default=2, help="how many stored days to check")
    p.set_defaults(func=cmd_verify_candles)

    p = sub.add_parser("diagnose", help="measure padding in stored candle data")
    p.add_argument("symbol")
    p.add_argument("timeframe", nargs="?", default="1min")
    p.set_defaults(func=cmd_diagnose)

    p = sub.add_parser("repair", help="strip padded minutes from stored bars")
    p.add_argument("symbol")
    p.add_argument("--timeframe", help="default: rebuild every timeframe")
    p.add_argument("--dry-run", action="store_true", help="report without writing")
    p.set_defaults(func=cmd_repair)

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
