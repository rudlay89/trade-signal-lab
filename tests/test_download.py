"""Download resilience: throttling, retries and resuming.

Dukascopy throttles sustained downloading and answers 503. That is the normal
case, not an exceptional one, so the behaviour around it is tested as carefully
as the happy path. Nothing here touches the network.
"""

from datetime import date, datetime, timezone

import pytest

from tsl.data import dukascopy
from tsl.data.dukascopy import (
    DownloadError, SourceSpec, download_hour, iter_daily_ticks,
)
from tsl.data.store import DownloadManifest
from tsl.data.synthetic import synthetic_bi5, synthetic_ticks

UTC = timezone.utc
EURUSD = SourceSpec("EURUSD", 100000.0, 0.5, 2.0)
HOUR = datetime(2024, 1, 8, 3, tzinfo=UTC)


class FakeResponse:
    def __init__(self, status_code, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}


class FakeSession:
    """Replays a scripted list of responses and records the URLs asked for."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        item = self._responses.pop(0) if self._responses else FakeResponse(200, b"")
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    """Retries wait tens of seconds by design. Record them, never serve them."""
    slept = []
    monkeypatch.setattr(dukascopy.time, "sleep", lambda s: slept.append(s))
    return slept


# --- retry behaviour ---------------------------------------------------------


def test_throttling_is_retried_then_succeeds(no_sleeping):
    """The exact failure seen in the field: a run of 503s, then recovery."""
    session = FakeSession([
        FakeResponse(503), FakeResponse(503), FakeResponse(200, b"payload"),
    ])
    assert download_hour(session, "EURUSD", HOUR) == b"payload"
    assert len(session.urls) == 3


def test_backoff_is_patient_not_quick(no_sleeping):
    """5s, 10s, 20s... The original 2/4/8 gave up inside fifteen seconds, which
    is far too soon for a throttle that clears in tens."""
    session = FakeSession([FakeResponse(503)] * 3 + [FakeResponse(200, b"ok")])
    download_hour(session, "EURUSD", HOUR)
    assert no_sleeping == [5.0, 10.0, 20.0]


def test_backoff_is_capped(no_sleeping):
    session = FakeSession([FakeResponse(503)] * 5 + [FakeResponse(200, b"ok")])
    download_hour(session, "EURUSD", HOUR, attempts=8, backoff=5.0, max_backoff=30.0)
    assert max(no_sleeping) == 30.0


def test_retry_after_header_wins_when_it_asks_for_longer(no_sleeping):
    session = FakeSession([
        FakeResponse(503, headers={"Retry-After": "90"}), FakeResponse(200, b"ok"),
    ])
    download_hour(session, "EURUSD", HOUR)
    assert no_sleeping == [90.0]


def test_unparseable_retry_after_falls_back_to_our_backoff(no_sleeping):
    session = FakeSession([
        FakeResponse(503, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        FakeResponse(200, b"ok"),
    ])
    download_hour(session, "EURUSD", HOUR)
    assert no_sleeping == [5.0]


def test_giving_up_explains_what_to_do(no_sleeping):
    session = FakeSession([FakeResponse(503)] * 6)
    with pytest.raises(DownloadError, match="throttling"):
        download_hour(session, "EURUSD", HOUR)


def test_missing_hour_is_not_an_error(no_sleeping):
    """404 means the market was closed. Retrying it would stall every weekend."""
    session = FakeSession([FakeResponse(404)])
    assert download_hour(session, "EURUSD", HOUR) == b""
    assert len(session.urls) == 1


def test_non_retryable_status_fails_immediately(no_sleeping):
    session = FakeSession([FakeResponse(403)])
    with pytest.raises(DownloadError, match="403"):
        download_hour(session, "EURUSD", HOUR)
    assert len(session.urls) == 1


def test_connection_errors_are_retried(no_sleeping):
    import requests

    session = FakeSession([requests.ConnectionError("reset"), FakeResponse(200, b"ok")])
    assert download_hour(session, "EURUSD", HOUR) == b"ok"


# --- day-at-a-time yielding --------------------------------------------------


def _payload_for(hour):
    ticks = synthetic_ticks(hour, hours=1.0, ticks_per_hour=20, seed=hour.hour)
    return synthetic_bi5(ticks, hour, EURUSD.point_scale)


def test_days_are_yielded_one_at_a_time(no_sleeping):
    """So the caller can persist each day as it arrives, rather than losing
    everything when the download is interrupted."""
    start = datetime(2024, 1, 8, tzinfo=UTC)
    end = datetime(2024, 1, 11, tzinfo=UTC)
    session = FakeSession([FakeResponse(200, _payload_for(HOUR))] * 72)

    days = list(iter_daily_ticks(EURUSD, start, end, session=session, pause=0))
    assert [d.date() for d, _ in days] == [date(2024, 1, 8), date(2024, 1, 9), date(2024, 1, 10)]
    assert all(not ticks.empty for _, ticks in days)
    assert len(session.urls) == 72


def test_a_failure_partway_keeps_the_days_already_yielded(no_sleeping):
    """The bug this replaced: two days downloaded, then a 503, and all of it
    discarded because nothing was written until the very end."""
    start = datetime(2024, 1, 8, tzinfo=UTC)
    end = datetime(2024, 1, 11, tzinfo=UTC)
    session = FakeSession([FakeResponse(200, _payload_for(HOUR))] * 48 + [FakeResponse(503)] * 6)

    collected = []
    with pytest.raises(DownloadError):
        for day, ticks in iter_daily_ticks(EURUSD, start, end, session=session, pause=0):
            collected.append(day.date())

    assert collected == [date(2024, 1, 8), date(2024, 1, 9)]


def test_skip_days_are_never_requested(no_sleeping):
    start = datetime(2024, 1, 8, tzinfo=UTC)
    end = datetime(2024, 1, 11, tzinfo=UTC)
    session = FakeSession([FakeResponse(200, _payload_for(HOUR))] * 48)

    days = list(iter_daily_ticks(
        EURUSD, start, end, session=session, pause=0, skip_days={date(2024, 1, 8)}
    ))
    assert [d.date() for d, _ in days] == [date(2024, 1, 9), date(2024, 1, 10)]
    assert len(session.urls) == 48  # not 72
    assert not any("/08/" in u for u in session.urls)


def test_an_empty_day_still_yields(no_sleeping):
    """A closed market must be reported, so it can be recorded as done and not
    re-requested on every resume."""
    start = datetime(2024, 1, 6, tzinfo=UTC)   # Saturday
    end = datetime(2024, 1, 7, tzinfo=UTC)
    session = FakeSession([FakeResponse(404)] * 24)

    days = list(iter_daily_ticks(EURUSD, start, end, session=session, pause=0))
    assert len(days) == 1
    assert days[0][1].empty


# --- manifest ----------------------------------------------------------------


def test_manifest_round_trip(tmp_path):
    manifest = DownloadManifest(tmp_path)
    assert manifest.completed_days("EURUSD") == set()

    manifest.mark_complete("EURUSD", date(2024, 1, 8))
    manifest.mark_complete("EURUSD", date(2024, 1, 9))
    assert manifest.completed_days("EURUSD") == {date(2024, 1, 8), date(2024, 1, 9)}


def test_manifest_is_per_instrument(tmp_path):
    manifest = DownloadManifest(tmp_path)
    manifest.mark_complete("EURUSD", date(2024, 1, 8))
    assert manifest.completed_days("XAUUSD") == set()


def test_marking_the_same_day_twice_is_harmless(tmp_path):
    manifest = DownloadManifest(tmp_path)
    manifest.mark_complete("EURUSD", date(2024, 1, 8))
    manifest.mark_complete("EURUSD", date(2024, 1, 8))
    assert manifest.completed_days("EURUSD") == {date(2024, 1, 8)}


def test_a_corrupt_manifest_costs_a_re_download_not_a_crash(tmp_path):
    manifest = DownloadManifest(tmp_path)
    path = manifest.path_for("EURUSD")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json")
    assert manifest.completed_days("EURUSD") == set()


# --- the download command end to end -----------------------------------------
#
# Exercises the real store, manifest and resume logic, with only the single HTTP
# call replaced. This is the path the user actually runs.


@pytest.fixture
def fake_feed(monkeypatch):
    """Serve real synthetic payloads, failing on demand at a given hour count."""
    state = {"calls": 0, "fail_after": None}

    def fake_download_hour(session, symbol, hour, **kwargs):
        state["calls"] += 1
        if state["fail_after"] is not None and state["calls"] > state["fail_after"]:
            raise DownloadError(f"{symbol} {hour}: HTTP 503 (simulated throttle)")
        ticks = synthetic_ticks(hour, hours=1.0, ticks_per_hour=60, seed=hour.hour)
        return synthetic_bi5(ticks, hour, EURUSD.point_scale)

    monkeypatch.setattr(dukascopy, "download_hour", fake_download_hour)
    return state


def test_download_stores_bars_and_records_the_days(fake_feed, tmp_path, capsys):
    from tsl.cli import main
    from tsl.data.store import BarStore

    code = main([
        "--data", str(tmp_path), "download", "EURUSD",
        "2024-01-08", "2024-01-10", "--pause", "0",
    ])
    assert code == 0, capsys.readouterr().out

    bars = BarStore(tmp_path).read("EURUSD", "1min")
    assert not bars.empty
    assert DownloadManifest(tmp_path).completed_days("EURUSD") == {
        date(2024, 1, 8), date(2024, 1, 9),
    }
    # Roll-ups are built at the same time.
    assert not BarStore(tmp_path).read("EURUSD", "15min").empty
    assert not BarStore(tmp_path).read("EURUSD", "4h").empty


def test_throttling_partway_keeps_the_finished_day(fake_feed, tmp_path, capsys):
    """THE FIELD FAILURE. A 503 on day two must not discard day one."""
    from tsl.cli import main
    from tsl.data.store import BarStore

    fake_feed["fail_after"] = 30  # one full day, then partway into the second

    code = main([
        "--data", str(tmp_path), "download", "EURUSD",
        "2024-01-08", "2024-01-11", "--pause", "0",
    ])
    assert code == 3

    out = capsys.readouterr().out
    assert "Nothing is lost" in out
    assert "Saved before stopping: 1 day" in out

    assert DownloadManifest(tmp_path).completed_days("EURUSD") == {date(2024, 1, 8)}
    assert not BarStore(tmp_path).read("EURUSD", "1min").empty


def test_rerunning_resumes_instead_of_starting_over(fake_feed, tmp_path, capsys):
    from tsl.cli import main

    fake_feed["fail_after"] = 30
    main(["--data", str(tmp_path), "download", "EURUSD", "2024-01-08", "2024-01-11", "--pause", "0"])

    # Second run: the throttle has cleared.
    fake_feed["fail_after"] = None
    fake_feed["calls"] = 0
    capsys.readouterr()

    code = main(["--data", str(tmp_path), "download", "EURUSD", "2024-01-08", "2024-01-11", "--pause", "0"])
    out = capsys.readouterr().out

    assert code == 0, out
    assert "1 day(s) already downloaded and will be skipped" in out
    # 48 hours for the two remaining days, not 72 for all three.
    assert fake_feed["calls"] == 48
    assert DownloadManifest(tmp_path).completed_days("EURUSD") == {
        date(2024, 1, 8), date(2024, 1, 9), date(2024, 1, 10),
    }


def test_a_fully_downloaded_range_does_nothing_the_second_time(fake_feed, tmp_path, capsys):
    from tsl.cli import main

    main(["--data", str(tmp_path), "download", "EURUSD", "2024-01-08", "2024-01-10", "--pause", "0"])
    fake_feed["calls"] = 0
    capsys.readouterr()

    assert main(["--data", str(tmp_path), "download", "EURUSD", "2024-01-08", "2024-01-10", "--pause", "0"]) == 0
    assert "already stored" in capsys.readouterr().out
    assert fake_feed["calls"] == 0


def test_restart_forces_a_re_download(fake_feed, tmp_path, capsys):
    from tsl.cli import main

    main(["--data", str(tmp_path), "download", "EURUSD", "2024-01-08", "2024-01-10", "--pause", "0"])
    fake_feed["calls"] = 0

    main(["--data", str(tmp_path), "download", "EURUSD", "2024-01-08", "2024-01-10",
          "--pause", "0", "--restart"])
    assert fake_feed["calls"] == 48
