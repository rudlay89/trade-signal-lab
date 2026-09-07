"""The Parquet bar store.

Layout:

    <root>/bars/<SYMBOL>/<TIMEFRAME>/<YYYY-MM>.parquet

One file per instrument, timeframe and month. Small enough to rewrite cheaply
when a month is topped up, large enough that a multi-year read is not thousands
of file opens. No database: a local Parquet lake handles years of 1-minute bars
comfortably and has no server to run or keep alive.

Writes are idempotent. Re-importing an overlapping range replaces the rows it
covers rather than duplicating them, so a download can be interrupted and
resumed without corrupting the store.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .bars import BAR_COLUMNS, empty_bars


class BarStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def path_for(self, symbol: str, timeframe: str, period: pd.Timestamp) -> Path:
        return (
            self.root / "bars" / symbol / timeframe / f"{period.year:04d}-{period.month:02d}.parquet"
        )

    def write(self, symbol: str, timeframe: str, bars: pd.DataFrame) -> list[Path]:
        """Merge `bars` into the store, one file per calendar month.

        Existing rows with the same timestamp are overwritten - a re-download is
        assumed to be a correction, not a duplicate.
        """
        if bars.empty:
            return []

        _validate(bars)
        written: list[Path] = []

        for (year, month), chunk in bars.groupby([bars.index.year, bars.index.month]):
            path = self.path_for(symbol, timeframe, pd.Timestamp(year=year, month=month, day=1))
            path.parent.mkdir(parents=True, exist_ok=True)

            if path.exists():
                existing = pd.read_parquet(path)
                existing.index = pd.to_datetime(existing.index, utc=True)
                combined = pd.concat([existing, chunk])
                combined = combined[~combined.index.duplicated(keep="last")].sort_index()
            else:
                combined = chunk.sort_index()

            combined.index.name = "timestamp"
            combined.to_parquet(path, index=True)
            written.append(path)

        return written

    def read(
        self,
        symbol: str,
        timeframe: str,
        start: pd.Timestamp | None = None,
        end: pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        """Read bars in [start, end). Missing months are simply absent, not an error."""
        directory = self.root / "bars" / symbol / timeframe
        if not directory.exists():
            return empty_bars()

        frames = []
        for path in sorted(directory.glob("*.parquet")):
            frame = pd.read_parquet(path)
            frame.index = pd.to_datetime(frame.index, utc=True)
            frames.append(frame)

        if not frames:
            return empty_bars()

        out = pd.concat(frames).sort_index()
        out = out[~out.index.duplicated(keep="last")]
        if start is not None:
            out = out[out.index >= pd.Timestamp(start, tz="UTC") if pd.Timestamp(start).tzinfo is None else out.index >= start]
        if end is not None:
            out = out[out.index < (pd.Timestamp(end, tz="UTC") if pd.Timestamp(end).tzinfo is None else end)]
        out.index.name = "timestamp"
        return out

    def coverage(self, symbol: str, timeframe: str) -> tuple[pd.Timestamp, pd.Timestamp] | None:
        """First and last stored bar timestamp, or None if nothing is stored."""
        bars = self.read(symbol, timeframe)
        if bars.empty:
            return None
        return bars.index[0], bars.index[-1]


def _validate(bars: pd.DataFrame) -> None:
    missing = set(BAR_COLUMNS) - set(bars.columns)
    if missing:
        raise ValueError(f"bars is missing column(s): {', '.join(sorted(missing))}")
    if not isinstance(bars.index, pd.DatetimeIndex):
        raise ValueError("bars must be indexed by timestamp")
    if bars.index.tz is None:
        raise ValueError("bar index must be timezone-aware UTC; naive timestamps are ambiguous")


class DownloadManifest:
    """Which days have been fully downloaded, per instrument.

    A day is recorded only after every hour in it has been fetched and stored,
    so a day in the manifest is complete by construction. That makes resuming
    safe: anything listed can be skipped without checking it again.

    The manifest also records days that legitimately held no ticks - weekends and
    holidays. Without that, every resume would re-request them forever, since
    "no bars stored" and "not downloaded yet" are otherwise indistinguishable.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def path_for(self, symbol: str) -> Path:
        return self.root / "manifest" / f"{symbol}.json"

    def completed_days(self, symbol: str) -> set:
        import json
        from datetime import date

        path = self.path_for(symbol)
        if not path.exists():
            return set()
        try:
            raw = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            # A corrupt manifest costs re-downloading, not correctness. Never let
            # it abort a run.
            return set()
        return {date.fromisoformat(d) for d in raw.get("completed_days", [])}

    def mark_complete(self, symbol: str, day) -> None:
        import json

        done = self.completed_days(symbol)
        done.add(day)
        path = self.path_for(symbol)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"symbol": symbol, "completed_days": sorted(d.isoformat() for d in done)}, indent=1)
        )
