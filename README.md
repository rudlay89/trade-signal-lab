# trade-signal-lab

A research-first toolkit for analysing intraday markets (FX, metals, indices, crypto) and
producing structured trade proposals: **instrument, direction, entry, stop-loss, take-profit,
position size, and a confidence score** — with every proposal traceable back to a rule that
has been backtested and walk-forward validated.

**Status:** early build. Data pipeline and position sizing are implemented and
tested; no strategies or backtesting yet.

**Shape so far:** signals and Telegram alerts only (no auto-execution), on EURUSD, GBPUSD,
USDJPY, AUDUSD and XAUUSD (XAGUSD researched but not tradeable at current account size),
intraday on a 15m signal timeframe, targeting the London morning session (14:00–19:00 Perth
time), sized against a $1,000 account risking 1% per trade, on free data at £0 running cost.
See [`docs/DECISIONS.md`](docs/DECISIONS.md).

## Documents

| Doc | What's in it |
| --- | --- |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | **Start here.** What's been decided, why, and what's still open |
| [`docs/STRATEGY-SPEC.md`](docs/STRATEGY-SPEC.md) | The exact trading rules in plain English — read this and push back |
| [`docs/POSITION-SIZING-REALITY-CHECK.md`](docs/POSITION-SIZING-REALITY-CHECK.md) | Why a $1,000 account at 1% risk can't trade silver, and what to do about it |
| [`docs/PAIRING-EXPLAINED.md`](docs/PAIRING-EXPLAINED.md) | Directional trading vs. statistical pairs trading, in plain English with worked examples |
| [`docs/RESEARCH.md`](docs/RESEARCH.md) | Landscape review: data vendors, broker APIs, backtesting engines, strategy families, and what actually works vs. what doesn't |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Proposed layered design, the signal contract, and a phased build plan |
| [`docs/OPEN-QUESTIONS.md`](docs/OPEN-QUESTIONS.md) | The full question list, including ones not yet answered |

## Getting started

Requires Python 3.11 or newer.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Then:

```bash
# What is configured, and why silver is excluded
.venv/bin/python -m tsl instruments

# What would this trade actually cost? (entry, stop)
.venv/bin/python -m tsl size EURUSD 1.10000 1.09800
.venv/bin/python -m tsl size XAUUSD 2412.50 2400.50    # watch this one get refused

# Download tick data and store it as bars (start inclusive, end exclusive).
# Start small - one month is plenty to check everything works.
.venv/bin/python -m tsl download EURUSD 2024-01-01 2024-02-01

# Is the stored data trustworthy?
.venv/bin/python -m tsl check EURUSD 15min
```

Run the tests with `.venv/bin/python -m pytest`.

### First download: what to expect

The Dukascopy decoder has **one unverified assumption** - the factor each
instrument's prices are scaled by. It could not be checked against real data
while being written, because the datafeed host is blocked by network policy in
the authoring environment.

That is handled rather than hoped away: every decoded price is checked against a
plausible range, and the import aborts with an explanatory message rather than
storing anything questionable. A wrong scale factor is wrong by a factor of ten
or more, so your first download of each instrument will either look right or fail
loudly and tell you which number to change. The metals are the least certain.

### Downloads are resumable, and will be interrupted

Dukascopy throttles sustained downloading and answers HTTP 503 when it does.
This is normal, not a fault, and it is designed for rather than treated as an
error:

* Each day is stored as soon as it is fetched, so an interruption costs time and
  never work.
* Completed days are recorded in a manifest. **Re-run the identical command** and
  it picks up where it stopped.
* Retries are patient - 5s, 10s, 20s, 40s, 80s - because a throttle clears in
  tens of seconds, not in two.

If you are being throttled persistently, slow down: `--pause 1.5` puts a second
and a half between requests. It takes longer and gets there.

### Two download paths

* **Daily candle files** (default) — 2 requests per instrument-day. Verified
  against tick-derived bars: open and close match exactly, high and low within
  about one spread.
* **Tick files** (`--source ticks`) — 24 requests per instrument-day, full
  sub-minute detail. Verified for EURUSD and XAUUSD against live data.

Both produce identical 1-minute bars. Candles are the default because throttling,
not bandwidth, is what makes a multi-year download slow — a year of five
instruments is about 3,700 requests instead of 44,000.

`tsl verify-candles SYMBOL` re-proves the candle format against any tick data you
already hold, for four requests. Worth running once per instrument.

## Guiding principles

1. **Validation before signals.** A signal generator with no walk-forward evidence is a
   random number generator with extra steps. The backtest harness gets built first.
2. **Costs are the strategy.** Spread, commission, slippage and swap decide whether an
   intraday edge survives. They are modelled from day one, not bolted on later.
3. **Risk sizing is not an afterthought.** Entry/SL/TP are derived from measured volatility
   (e.g. ATR multiples), and position size follows from a fixed fractional risk budget.
4. **Explainable output.** Every proposal states which rule fired, on what evidence, and
   what its historical hit rate and expectancy were.

## Disclaimer

This is research software. It does not provide financial advice, and nothing it outputs is a
recommendation to trade. Intraday leveraged trading in FX and CFDs loses money for the large
majority of retail accounts; brokers in most regulated jurisdictions are required to publish
that figure and it typically sits in the 65–85% range (moderate confidence — verify against
your own broker's current disclosure). Treat any positive backtest as a hypothesis, not a
result, until it has survived out-of-sample and forward testing.
