# Research: building an intraday signal engine for FX, metals and related instruments

Written 2026-09-05. Confidence flags are on claims where it matters: facts sourced from this
round of research are marked, and anything recalled rather than verified says so.

---

## 1. The honest framing

Before any architecture: the hard part of this project is **not** the code. Computing an ATR
stop and a 2R target is an afternoon's work. The hard parts, in order of difficulty:

1. **Finding an edge that survives costs.** On EURUSD an intraday move might be 40–60 pips;
   a typical retail spread plus commission eats 1–2 pips per round trip. On XAUUSD spreads are
   far wider and more variable, especially around news. A strategy with a genuine 0.05R
   expectancy before costs is a losing strategy after them.
2. **Not fooling yourself.** If you scan 20 instruments × 5 timeframes × 50 parameter
   combinations, you have run 5,000 experiments. Some will look excellent by luck alone. This
   is the multiple-comparisons problem and it is the single most common way retail systematic
   projects fail.
3. **Regime change.** An edge fitted to 2023–2024 range conditions can invert in a trending
   2026. Walk-forward validation and live monitoring of degradation are mandatory, not nice-to-have.

The tool is therefore best framed as a **research platform that happens to emit signals**,
rather than a signal generator you hope is right. That framing drives the architecture in §5.

---

## 2. Market data

Two distinct needs, often served by different vendors:

**(a) Deep historical intraday data for backtesting** — needs years of 1-minute or tick bars,
ideally with bid/ask so spread can be modelled rather than assumed.

**(b) Live/near-live data for signal generation** — needs low latency and reliable uptime, but
only a rolling window of history.

### Candidate sources

| Source | Coverage | Cost | Notes |
| --- | --- | --- | --- |
| **Dukascopy** historical data export | FX, metals, indices, CFDs; tick-level; history back to ~1990–2000 depending on instrument | Free | Strong candidate for backtest data. Bid/ask ticks included. Python downloaders exist (`dukascopy-python`, `duka`); `dukascopy-node` is the most actively maintained per this research. Caveat: it is *Dukascopy's* liquidity, not your broker's — spreads will differ. |
| **HistData.com** | FX majors, 1-min bars | Free | Older, simpler, no bid/ask spread detail. Fallback only. |
| **Twelve Data** | FX, metals, stocks, crypto; intraday + 60-odd precomputed indicators | Free tier → paid tiers up to roughly $329/mo at the top (moderate confidence on the figure — pricing changes; verify) | Good developer ergonomics, single API for several asset classes. Reasonable choice for live data. |
| **Polygon.io** (reportedly rebranded "Massive" in 2026 — *low-to-moderate confidence, single source*) | Excellent US equities/options; **forex coverage is comparatively thin** | Combined stocks+options+forex advanced plan reported around $449/mo (moderate confidence) | Probably wrong shape for an FX/metals-first tool. |
| **Databento** | Institutional futures/options: CME, COMEX, NYMEX — i.e. **GC (gold) and SI (silver) futures**, L2/L3 order book | Consumption-based billing | The serious option if you want exchange-traded metals rather than spot CFDs. Overkill for a first build. |
| **Broker's own API** (see §3) | Whatever you can trade | Included | The only feed whose prices match what you'd actually fill at. Usually limited history. |
| Metals-only APIs (GoldAPI.io, MetalpriceAPI, UniRateAPI, api-ninjas) | Spot gold/silver, often LBMA reference | Free tiers, ~$9/mo and up | Mostly *spot reference prices*, often low frequency. Fine for a dashboard, generally **not** granular enough for intraday backtesting. |

**Recommendation:** Dukascopy for historical/backtest, broker API for live. Twelve Data as a
convenient cross-asset live fallback. This costs £0 to start.

### The data trap to avoid
Spot FX and metals CFDs have **no consolidated tape** — there is no single "correct" EURUSD
price. Every venue's data differs slightly. So: backtest on one feed, trade on another, and
your results silently drift. Mitigation is to (a) model costs conservatively, and (b) run a
forward-test on the *live* feed before believing anything.

---

## 3. Execution / broker connectivity

| Option | Fit | Notes |
| --- | --- | --- |
| **OANDA v20 REST API** | Strong | Clean REST + streaming prices, mature Python ecosystem, well documented, regulated in multiple jurisdictions. Research consistently flags it as the pragmatic pick for retail FX algo work. Not suitable for sub-millisecond HFT — irrelevant for our timeframes. Metals available as CFDs. |
| **MetaTrader 5 + Python** | Viable, awkward | Enormous broker support and the industry default. **The official `MetaTrader5` Python package is Windows-only** (high confidence — corroborated). Linux use requires Wine + RPC shims (`mt5linux`, `pymt5linux`) or a Docker image running MT5 under Wine with VNC. Workable, but a moving part that will break at inconvenient moments. |
| **MetaApi (cloud MT4/MT5)** | Viable | Hosted bridge to MT5, removes the Wine problem, adds a subscription and a third-party dependency. |
| **Interactive Brokers** | Good if you want breadth | Real futures (GC/SI), equities, FX. Heavier API, but genuine exchange execution rather than CFD. |
| **cTrader Open API** | Good | Modern API, growing broker support. Less Python material than OANDA. |
| **Paper/simulation only** | Best starting point | Costs nothing, risks nothing, and is where this project should live for its first several months. |

**Recommendation:** design an `ExecutionAdapter` interface with a paper-trading implementation
first, then OANDA. Keep MT5 as a later adapter if a specific broker requires it.

---

## 4. Backtesting engines

From this research round:

- **vectorbt** — vectorised, NumPy/pandas/Numba. Built for sweeping thousands of parameter
  combinations fast. Best for the *research* phase: parameter sweeps, walk-forward grids,
  robustness surfaces. Open-source version is capable; there is a paid PRO tier.
- **NautilusTrader** — event-driven, models order handling, latency and fills realistically;
  designed to run the *same* strategy code in backtest and live. Best for the *validation and
  live* phase. Heavier to learn.
- **Backtrader** — long the retail default, but reported to have entered long-term maintenance
  mode around 2023 with no major features planned (moderate confidence — from secondary
  sources, not the maintainer directly). I'd avoid starting new work on it.
- **backtesting.py** — small, readable, single-instrument. Genuinely useful for a first
  prototype before committing to a bigger framework. (Recalled, not verified this round.)
- **Roll your own** — for a bar-close-only, one-position-at-a-time intraday system, a correct
  event loop is a few hundred lines and you control the cost model exactly. Tempting, and the
  main risk is subtle look-ahead bias.

**Recommendation:** `vectorbt` for research sweeps + `NautilusTrader` for the
validation/live path. If that's too much surface area to start, a small purpose-built event
loop with a rigorous cost model, then port to Nautilus once the strategy set stabilises.

---

## 5. Strategy families worth testing

You mentioned "pairing" — that's ambiguous between two quite different things, and it's worth
being explicit because they demand different machinery:

### (a) Statistical pairs / spread trading (mean reversion on a relationship)
Trade the *spread* between two co-moving instruments rather than either outright. Classic
approach: test for cointegration (Engle–Granger 1987 is the standard reference — high
confidence), fit a hedge ratio, z-score the residual, enter at ±2σ, exit at 0σ.

Natural candidates in your universe:
- **XAUUSD / XAGUSD** — the gold/silver ratio is the textbook metals spread.
- **AUDUSD / NZDUSD**, **EURUSD / GBPUSD**, **USDCAD / oil** — commodity- and region-linked FX.
- **Triangular relationships** — EURUSD × USDJPY vs EURJPY. Genuine arbitrage here is gone
  (HFT owns it), but the *statistical* deviation is still tradeable at slower speeds.

Appeal: roughly market-neutral, so it survives directional regime change better.
Caveat: literature returns for intraday pairs trading look attractive (published Sharpe figures
above 2 appear in the papers surfaced this round) — but published results are heavily subject
to survivorship and selection bias, and rarely include realistic retail costs. Treat as a
starting hypothesis, not a target.

### (b) Directional intraday setups (what most people mean by "day trading signals")
- **Session breakout** — Asian-range breakout at the London open; London-range breakout at NY.
  Strong prior: FX volatility is highly session-dependent, and the London/NY overlap carries
  the most.
- **Trend-pullback** — trend filter on a higher timeframe, entry on a pullback to a moving
  average or Fibonacci level on a lower one.
- **VWAP / mean reversion** — fade extensions from session VWAP inside a range regime.
- **Volatility-regime switching** — an explicit regime classifier (ADX, ATR percentile, realised
  vol) that decides *which* of the above is allowed to fire today. This is often worth more
  than any individual entry rule.
- **News/event filters** — suppress signals around high-impact releases (NFP, CPI, FOMC), where
  spreads widen and stops get hit on noise. An economic calendar feed is a required input.

### (c) ML overlays
Feasible, but the right sequencing is: rule-based baseline first, then ML as a *filter* on
those signals (predicting "will this setup reach 1R before -1R?"), not as an end-to-end price
predictor. End-to-end price prediction from OHLCV is the most reliably disappointing thing in
this field. Labelling wants the triple-barrier method (López de Prado, *Advances in Financial
Machine Learning*, 2018 — high confidence on the reference).

---

## 6. Turning an analysis into entry / SL / TP

This is the part that makes the tool concretely useful, and it should be a **separate,
strategy-agnostic layer**:

- **Entry** — explicit trigger type: market on bar close, stop order beyond a level, or limit
  at a retracement. Plus a validity window, so a stale signal expires rather than filling hours late.
- **Stop-loss** — volatility-scaled (e.g. `entry ∓ k × ATR(14)`), or structural (beyond the swing
  high/low, or the range boundary), whichever is wider. Fixed-pip stops are the classic beginner
  error: they mean something completely different on XAUUSD at 8% annualised vol than at 25%.
- **Take-profit** — expressed in **R multiples** (R = the distance to stop). A 1.5–3R target with
  partials and a move to breakeven is the standard shape. The critical constraint: **win rate and
  R:R are not independent** — pushing targets further mechanically lowers hit rate. The engine
  should report expectancy, not either number in isolation.
- **Position size** — `size = (account_equity × risk_pct) / (stop_distance × pip_value)`. This is
  where account currency, contract size and instrument-specific pip value all have to be right,
  and where an off-by-one becomes expensive.
- **Portfolio-level guards** — max concurrent positions, max correlated exposure (three long-USD
  trades is one trade in three costumes), daily loss limit, kill switch.

---

## 7. Validation methodology (non-negotiable)

1. **In-sample / out-of-sample split**, chronological, never random-shuffled.
2. **Walk-forward analysis** — repeatedly fit on a rolling window, test on the next unseen
   window. This is the closest offline proxy to live performance.
3. **Realistic cost model** — spread (time-varying, wider at session open/close and around news),
   commission, slippage on stop orders, and swap/financing on anything held overnight.
4. **Multiple-testing correction** — track how many configurations were tried; deflate the
   observed Sharpe accordingly. (Deflated Sharpe Ratio, López de Prado — moderate confidence on
   exact formulation, high confidence the concept applies.)
5. **Monte Carlo / trade-order shuffling** — to estimate the realistic drawdown distribution,
   not just the one path that happened.
6. **Forward paper trading** — minimum a few months on live data before any real capital.

If a strategy can't clear all six, it doesn't get to emit signals.

---

## 8. Proposed stack

- **Python 3.12+** — the whole quant ecosystem lives here.
- **Data**: `pandas` / `polars`, `pyarrow` + **Parquet** files partitioned by
  instrument/date. A local Parquet lake handles years of 1-min bars without a database. Add
  DuckDB for querying it if needed; consider TimescaleDB only if a live tick store is required.
- **Analysis**: `numpy`, `scipy`, `statsmodels` (cointegration tests, ADF), `pandas-ta` or
  `TA-Lib` for indicators.
- **Backtest**: `vectorbt` → `NautilusTrader` (per §4).
- **Orchestration**: APScheduler or Prefect for scheduled scans; simple cron is fine at first.
- **Interface**: start with CLI + a Markdown/HTML daily report. A Streamlit or FastAPI+React
  dashboard later if it earns its place. Alerting via Telegram bot is the low-friction option.
- **Deployment**: a small always-on VPS if signals need to fire during London/NY hours while
  you're not at the machine.

---

## 9. Suggested phasing

| Phase | Deliverable | Rough effort |
| --- | --- | --- |
| 0 | Repo, config, instrument spec registry (pip values, contract sizes, sessions, typical spreads) | small |
| 1 | Data ingestion: Dukascopy → Parquet lake, with resampling and data-quality checks (gaps, spikes, weekend handling) | small–medium |
| 2 | Backtest harness + cost model + metrics (expectancy, Sharpe, max DD, R-distribution) | medium |
| 3 | First strategy: one directional setup (session breakout) end-to-end, fully validated | medium |
| 4 | Risk/sizing layer producing the entry/SL/TP/size contract | small |
| 5 | Walk-forward + robustness tooling | medium |
| 6 | Live data + daily scan + report/alerts (signals only, no execution) | medium |
| 7 | Second strategy family: statistical pairs (metals + FX crosses) | medium |
| 8 | Paper-trading execution adapter, then live adapter behind a hard-off switch | medium |

Phases 0–4 are the minimum viable honest version.

---

## Sources consulted (2026-09-05)

- [Dukascopy Historical Data Export](https://www.dukascopy.com/swiss/english/marketwatch/historical/) · [dukascopy-node](https://github.com/Leo4815162342/dukascopy-node) · [dukascopy-python](https://pypi.org/project/dukascopy-python/) · [duka](https://giuse88.github.io/duka/)
- [Twelve Data Forex APIs](https://twelvedata.com/forex) · [GoldAPI.io](https://www.goldapi.io/) · [MetalpriceAPI](https://metalpriceapi.com/gold) · [UniRateAPI gold](https://unirateapi.com/gold-price-api)
- [Quant data provider comparison](https://waylandz.com/quant-book-en/Data-Provider-Comparison/) · [Futures data API comparison: Polygon vs Databento](https://www.edgeful.com/blog/posts/futures-data-api-polygon-databento-edgeful-comparison)
- [Best forex brokers with APIs 2026](https://www.forexbrokers.com/guides/best-api-brokers) · [OANDA v20 API review](https://forex-trading-daily.contentwave.net/article/oanda-v20-api-reviewed-practical-verdict-for-fx-algo-traders)
- [mt5linux](https://pypi.org/project/mt5linux) · [pymt5linux](https://pypi.org/project/pymt5linux) · [MetaTrader5-Docker](https://github.com/gmag11/MetaTrader5-Docker) · [MetaApi SDK](https://pypi.org/project/metaapi-cloud-sdk/27.0.4/)
- [Python backtesting landscape 2026](https://python.financial/) · [Best Python backtest engines 2026](https://bullalert.ai/blog/best-python-backtest-engines-2026/) · [Backtrader vs NautilusTrader vs VectorBT](https://autotradelab.com/blog/backtrader-vs-nautilusttrader-vs-vectorbt-vs-zipline-reloaded)
- [Intraday dynamic pairs trading (QuantConnect)](https://www.quantconnect.com/research/15347/intraday-dynamic-pairs-trading-using-correlation-and-cointegration-approach/) · [A survey of statistical arbitrage pairs (Univ. of Warsaw WP 19/2025)](https://www.wne.uw.edu.pl/download_file/6095/0) · [Designing pair-trading strategies using cointegration (arXiv 2211.07080)](https://arxiv.org/pdf/2211.07080)
