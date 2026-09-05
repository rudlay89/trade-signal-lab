# Proposed architecture

Provisional — several decisions depend on answers to [`OPEN-QUESTIONS.md`](OPEN-QUESTIONS.md).

## Layered pipeline

```
┌──────────────────────────────────────────────────────────────────────┐
│ 1. DATA                                                              │
│    Dukascopy / broker / Twelve Data  →  normalise  →  Parquet lake   │
│    quality checks: gaps, spikes, session boundaries, DST, weekends   │
└──────────────────────────────┬───────────────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 2. FEATURES                                                          │
│    indicators (ATR, ADX, RSI, VWAP, MAs), session ranges,            │
│    realised vol, correlation/cointegration matrices, calendar flags  │
└──────────────────────────────┬───────────────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 3. REGIME FILTER                                                     │
│    trending / ranging / high-vol / news-blackout                     │
│    decides which strategies are permitted to fire right now          │
└──────────────────────────────┬───────────────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 4. STRATEGIES  (plugins, one interface)                              │
│    session_breakout · trend_pullback · vwap_reversion ·              │
│    stat_pairs (cointegration spread) · ...                           │
│    each emits: direction + conviction + reference levels             │
└──────────────────────────────┬───────────────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 5. RISK & SIZING          ← the layer that makes output actionable   │
│    entry type/level · ATR- or structure-based SL · R-multiple TPs ·  │
│    position size from risk budget · correlation & exposure caps      │
└──────────────────────────────┬───────────────────────────────────────┘
                               ▼
              ┌────────────────┴────────────────┐
              ▼                                 ▼
┌───────────────────────────┐     ┌──────────────────────────────────┐
│ 6a. BACKTEST / WALK-FWD   │     │ 6b. LIVE SCAN                    │
│  cost model, metrics,     │     │  scheduled runs, signal store,   │
│  robustness, MC shuffling │     │  report + alerts                 │
└───────────────────────────┘     └──────────────┬───────────────────┘
                                                 ▼
                                  ┌──────────────────────────────────┐
                                  │ 7. EXECUTION ADAPTER (optional)  │
                                  │  paper → OANDA / MT5 / IBKR      │
                                  │  behind an explicit kill switch  │
                                  └──────────────┬───────────────────┘
                                                 ▼
                                  ┌──────────────────────────────────┐
                                  │ 8. JOURNAL & ANALYTICS           │
                                  │  every signal logged with        │
                                  │  outcome; live-vs-backtest drift │
                                  └──────────────────────────────────┘
```

The key structural idea: **strategies decide *what and where*; the risk layer decides *how much
and with what stop*.** Keeping those separate means one well-tested sizing implementation
serves every strategy, and strategies stay small and individually testable.

## The signal contract

Every strategy, in backtest and live, produces the same object. Sketch:

```python
@dataclass(frozen=True)
class Signal:
    # identity
    id: str
    generated_at: datetime          # UTC, always
    strategy: str
    instrument: str                 # "XAUUSD", "EURUSD"

    # the trade
    direction: Literal["long", "short"]
    entry_type: Literal["market", "stop", "limit"]
    entry_price: Decimal
    stop_loss: Decimal
    take_profits: list[TakeProfit]  # price + fraction of position to close
    valid_until: datetime           # signal expires; no stale fills

    # sizing (computed by the risk layer, not the strategy)
    risk_pct: Decimal               # of account equity
    position_size: Decimal          # lots / units for the configured account
    risk_reward: Decimal            # to the final TP

    # justification — so it can be judged, not just obeyed
    rationale: str                  # human-readable: which rule, what evidence
    features: dict[str, float]      # the indicator values at signal time
    regime: str
    historical_expectancy: Decimal  # this rule's backtested edge, in R
    sample_size: int                # how many historical trades that rests on
    confidence: Decimal             # 0-1, and honest about it
```

`historical_expectancy` + `sample_size` travelling with every signal is deliberate: it makes it
impossible to look at a suggestion without also seeing how much evidence stands behind it.

## Repository layout (proposed)

```
trade-signal-lab/
├── docs/
├── config/
│   ├── instruments.yaml      # pip value, contract size, sessions, typical spread
│   ├── strategies/           # one file per strategy's parameters
│   └── risk.yaml             # risk %, max positions, daily loss limit
├── src/tsl/
│   ├── data/                 # ingest, normalise, quality, store
│   ├── features/             # indicators, sessions, regimes, calendar
│   ├── strategies/           # plugins implementing one interface
│   ├── risk/                 # sizing, SL/TP construction, exposure caps
│   ├── backtest/             # engine, cost model, metrics, walk-forward
│   ├── live/                 # scheduler, scan, signal store
│   ├── execution/            # adapters: paper, oanda, mt5
│   └── report/               # markdown/HTML report, alerts
├── notebooks/                # exploratory research
├── tests/
└── pyproject.toml
```

## Cross-cutting rules

- **Everything in UTC internally**; convert only at the presentation edge. FX sessions and DST
  transitions are a rich source of off-by-one-hour bugs.
- **Decimal, not float, for prices and money.** Float rounding on a 5-decimal FX price
  compounds into wrong position sizes.
- **The same strategy code runs in backtest and live.** Any divergence between the two paths is
  where look-ahead bias hides.
- **No network calls inside strategy code.** Strategies get data handed to them; that keeps them
  deterministic and testable.
- **Every signal is persisted at generation time**, before the outcome is known — otherwise the
  journal becomes a record of the ones you remember.
