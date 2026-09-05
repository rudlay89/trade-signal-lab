# Decisions log

Answers given so far and what each one changes about the build.

---

## D1 — Scope: signals + live alerts, no auto-execution
The tool scans on a schedule during the trading window and pushes alerts as setups appear. It
never places an order. You place every trade yourself.

**Consequence:** no broker execution adapter needed, which removes the largest and riskiest
chunk of work (order management, partial fills, reconnection, kill switches). We still design
the `Signal` object so an execution adapter *could* be added later without rework.

## D2 — Instruments: FX majors + XAUUSD/XAGUSD
Starting universe: **EURUSD, GBPUSD, USDJPY, AUDUSD, XAUUSD, XAGUSD**. Six instruments.

**Consequence:** deliberately small. Six instruments × a handful of strategies is a number of
hypotheses we can honestly correct for. Twenty instruments would not be.
XAGUSD is in mainly so the gold/silver spread is available for phase 2 — silver's wider spreads
make it a poor standalone day-trading instrument.

## D3 — Timeframe: intraday, hours, flat by end of session
Signal timeframe 15m with a 1h/4h trend filter; backtests run on 1-minute data so intrabar
stop/target sequencing is modelled properly.

**Consequence:** no overnight swap/financing in the cost model. Costs are survivable (unlike
scalping). Free Dukascopy data is more than adequate.

## D4 — No broker yet, £0 budget
- **Historical data:** Dukascopy free tick/1-min export → local Parquet files.
- **Live data:** free tier of a REST provider (Twelve Data or similar), polled — not streamed.
- **Broker:** deferred. Once we know from backtests which instruments and spreads matter, we
  pick a broker against those requirements rather than guessing now.

**Consequence:** spreads must be *assumed conservatively* rather than measured from your own
account. The cost model will be deliberately pessimistic, and the first thing to re-validate
once you do open an account (even a demo) is whether real spreads match the assumption.

## D5 — "Pairing": start directional, add statistical pairs in phase 2
See [`PAIRING-EXPLAINED.md`](PAIRING-EXPLAINED.md).

## D6 — Trading window: London morning, from Perth
You're in **Perth (UTC+8, no daylight saving — high confidence, WA does not observe DST)** and
want the **London morning session, 07:00–12:00 UK**.

That maps to your local time as:

| UK period | London 07:00–12:00 UK is… | Perth local |
| --- | --- | --- |
| **British Summer Time** (UTC+1, ~late Mar – late Oct) | 06:00–11:00 UTC | **14:00–19:00** |
| **GMT** (UTC+0, ~late Oct – late Mar) | 07:00–12:00 UTC | **15:00–20:00** |

This is a good window for you — mid-afternoon to evening, not the middle of the night. It also
covers the Asian-range-breakout setup nicely, since the Asian session has just closed.

**Consequence (important):** the window **shifts by one hour twice a year** because the UK
observes DST and Perth doesn't. This is a classic source of silent bugs. Mitigations:
- store and compute everything in **UTC** internally;
- define the session in the config as a **London wall-clock time** (`Europe/London`) and let a
  proper timezone library resolve it, never as a hardcoded UTC offset;
- render alert timestamps in **both** London and `Australia/Perth` time so a mismatch is
  immediately visible;
- add a test that asserts the session boundaries are correct on both sides of a DST transition.

## D7 — Alerts via Telegram
A Telegram bot pushes signals to your phone. Setup is: message `@BotFather`, create a bot, get
a token, get your chat ID. Free, instant, no server needed to receive.

**Consequence:** message formatting matters a lot — the alert *is* the product. It needs to be
readable on a phone in ten seconds: instrument, direction, entry, SL, TP, size, and the rule's
historical track record.

## D8 — You're not a coder, and have no existing setup to encode
**Consequence — this shapes everything:**
- All tunable values live in **commented YAML config files**, never in code.
- One command to run it; a short plain-English setup guide.
- Backtest results come out as a **readable HTML/Markdown report with charts**, not a console
  dump of numbers.
- I propose and test the strategies (London breakout, trend-pullback, VWAP reversion), and I
  report honestly on which ones don't work. Expect at least one of them to fail — that's the
  system working correctly, not a setback.

## D9 — Risk: $1,000 USD account, 1% ($10) per trade
Account currency USD. Position sizing targets $10 of risk per trade.

**Consequence — significant.** At this size, broker minimum lot sizes collide with the risk
budget on the metals. Silver is not tradeable at all; gold is only tradeable on calm days. Full
arithmetic in [`POSITION-SIZING-REALITY-CHECK.md`](POSITION-SIZING-REALITY-CHECK.md).

Resolution: trade the four FX majors + XAUUSD, and build a rule into the sizing layer —
**if the minimum position size would risk more than the budget, skip the trade and log why.**
XAGUSD stays in the research universe but is flagged non-tradeable at current account size.
This is a config flag, so it flips the moment account size or broker granularity changes.

## D10 — Real money is the goal
**Consequence:** validation gets maximum rigour — walk-forward, multiple-testing correction,
Monte Carlo drawdown estimation, and a mandatory forward paper-trading period before any capital.
I will report backtest results straight, including when a strategy doesn't work.

Also worth stating: at $1,000 risking $10/trade, a genuinely good system returns a few hundred
dollars a year. The account is a validation vehicle, not an income source. That's the right way
to use it — see the closing section of the sizing doc.

## D11 — Runs on your own machine for now
One command, run when you sit down to trade. No hosting cost, no setup.

**Consequence:** the scheduler must be a simple foreground process that's obvious when it's
running and when it isn't. Hosting can move to a VPS later without touching strategy code.

## D12 — Silver dropped from the tradeable set
Trade EURUSD, GBPUSD, USDJPY, AUDUSD, XAUUSD. XAGUSD stays in the research universe but is
flagged non-tradeable. The sizing layer gets the rule: **minimum position size exceeds the risk
budget → skip the trade and log why.**

**Consequence:** the gold/silver spread is off the table for phase 2 until account size or broker
granularity changes. The FX spreads (AUDUSD/NZDUSD, EURUSD/GBPUSD) are unaffected — though NZDUSD
would need adding to the universe for the first of those.

## D13 — News blackout: 30 minutes either side of high-impact events
**Consequence — a real data problem, with a workable answer.** Free calendar feeds cover the
current and upcoming week, which serves live trading fine. Backtesting needs *historical* event
dates, which are much harder to get free. Resolution: generate historical events from their known
recurring schedule (NFP first Friday 08:30 NY, FOMC's eight scheduled meetings, US CPI mid-month,
ECB/BoE decision dates) rather than a feed. Approximate, but it covers what matters and costs
nothing. Live uses a free feed with the schedule as fallback.

We'll backtest with the filter both on and off so its value is measured, not assumed.

## D14 — Exits: partial at 2R, breakeven, remainder at 3R
Plus a hard time exit at 16:00 London.

**Consequence:** the backtest engine must model partial closes and stop modifications, and must
sequence intrabar events correctly — if a bar's range covers both the stop and the target, we
need 1-minute data to know which came first. Assuming the favourable one is a classic way to
manufacture a fake edge, so the engine resolves this from 1-minute bars and assumes the
unfavourable order when even that is ambiguous.

## D15 — Concepts: standard indicators + support/resistance + Fibonacci + candlestick triggers
No ICT/Smart Money concepts, which is my preference too — they're hard to define strictly enough
to backtest honestly.

**Consequence:** all of these need *objective* definitions, which is done in the strategy spec —
a swing point is "a bar higher than the two either side", not something identified by eye. This
is what makes them testable.

## D16 — Risk limits confirmed
- Max **3 open positions** at once.
- Max **2 positions sharing a dominant currency** — long EURUSD + long GBPUSD + short USDJPY is
  three ways of being short the dollar, not three independent trades.
- **Daily stop at −3R (−$30).** After that, no further signals until the next day.

## D17 — Success in 3 months = a profitable paper-trading month
Not a good backtest. A month of *forward* testing on live data, where the results hold up.

**Consequence — this changes what the tool has to do, not just what it reports:**
- The **paper-trading journal is a first-class feature**, not an afterthought. Every signal is
  logged when generated, then followed to its outcome automatically.
- The headline metric is **live-vs-backtest drift**: are real-time results tracking what the
  backtest predicted? Divergence is the earliest warning that an edge was overfitted, and it
  shows up long before the P&L makes it obvious.
- It sets a **timeline**: backtest work needs to be done early enough to leave a clear month of
  forward testing inside the three months. Roughly — foundations and validation in weeks 1–6,
  live signals running from week 7, and a clean paper month across weeks 8–12.
- It raises the bar on the live path. Backtest bugs waste time; live-scan bugs waste the month.
  The scan needs to be reliable enough that a missed signal is a real event, not routine.

## D18 — Standalone GitHub repository
Split out of `My-Personal-projects` into its own public repo, `rudlay89/trade-signal-lab`.

**Note on the public choice:** you picked the public option over the private one. That's fine for
what's here now — strategy rules and design docs. Two things to keep true as it grows: the
Telegram bot token and any broker credentials go in a `.env` file that is **never** committed
(`.gitignore` already covers it), and the paper-trading journal with your actual positions stays
local. If you later want the repo private, it's a one-click change in GitHub settings.

---

## Still open

| # | Question | Why it matters |
| --- | --- | --- |
| 1 | **Do the strategy rules match your instincts?** | [`STRATEGY-SPEC.md`](STRATEGY-SPEC.md) is written to be read and argued with. Changing rules now is free; changing them after they're built is not. |
| 2 | **Anything else in the docs you'd change?** | You're reading them now — that's exactly the right moment to push back. |
| 3 | **A broker, eventually** | Not needed to start, and no rush. But a *demo* account would let us measure real spreads instead of assuming pessimistic ones, and that should happen before the paper-trading month. |

### Hosting note (kept for when this moves off your machine)
- **Your own machine** — free, zero setup. Works if you're at the computer during 14:00–19:00 Perth.
- **Oracle Cloud Always Free** — still available in 2026, but Oracle *reduced* the Always Free ARM
  allowance in June 2026 to 2 OCPUs / 12 GB (moderate confidence — single source; verify current
  terms), and capacity in a given region is not guaranteed. Still the most generous free option.
- **GitHub Actions on a cron** — tempting because it's free and already where the code lives, but
  **not suitable for time-sensitive alerts**: the minimum interval is 5 minutes, scheduled runs
  are commonly delayed 5–30 minutes under load, and can be skipped entirely (high confidence —
  documented behaviour and widely reported). A breakout alert 20 minutes late is worse than no
  alert. Fine for a nightly data-download job; not for live signals.
