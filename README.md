# trade-signal-lab

A research-first toolkit for analysing intraday markets (FX, metals, indices, crypto) and
producing structured trade proposals: **instrument, direction, entry, stop-loss, take-profit,
position size, and a confidence score** — with every proposal traceable back to a rule that
has been backtested and walk-forward validated.

**Status:** design phase. Nothing is implemented yet.

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
