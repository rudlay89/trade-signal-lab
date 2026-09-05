# Strategy specification

Plain-English rules, precise enough to code and to backtest. Read this and push back on
anything that doesn't match your instincts — it's much cheaper to change now than after it's built.

Two strategies to start. Deliberately two, not six: every extra strategy is another set of
hypotheses to test, and with six instruments we're already running enough experiments that luck
starts to look like skill. More can be added once these are honestly measured.

**Tradeable universe:** EURUSD, GBPUSD, USDJPY, AUDUSD, XAUUSD.
XAGUSD is researched but not traded (see [`POSITION-SIZING-REALITY-CHECK.md`](POSITION-SIZING-REALITY-CHECK.md)).

---

## Shared rules (apply to every strategy)

### Session
All times London (`Europe/London`), resolved by timezone library so BST/GMT is automatic.
- **Signals may only be generated 07:00–16:00.**
- **Any open position is closed at 16:00** regardless of P&L. Nothing is held overnight.
- In your local time that's roughly 14:00–23:00 Perth (BST) or 15:00–24:00 (GMT). You won't be
  awake for the whole window — the 16:00 close is automatic in backtest, and in live use the
  alert will state the hard exit time so you can set it with the order.

### News blackout
No new signals from **30 minutes before to 30 minutes after** a high-impact event affecting
either currency in the instrument. Gold is treated as USD-sensitive.

*Data source problem, stated honestly:* free economic calendar feeds cover the current and
upcoming week, which is fine for live trading — but backtesting a news filter needs **historical**
event dates, and those are much harder to get free. The pragmatic solution:

- **Backtest:** generate events from their known recurring schedule rather than a data feed —
  US Non-Farm Payrolls (first Friday of the month, 08:30 New York time), FOMC rate decisions
  (eight scheduled meetings a year, 14:00 NY), US CPI (mid-month, 08:30 NY), plus ECB and BoE
  decision dates. High confidence on NFP timing and the eight-meetings-a-year figure; the exact
  CPI release day drifts, so it's approximate. This covers the events that actually matter and
  needs no data source.
- **Live:** a free calendar feed, cached locally, with the recurring schedule as a fallback if
  the feed is unavailable.

We'll backtest with the filter on and off, so we know what it's worth rather than assuming.

### Risk and sizing (every trade)
1. Risk budget = 1% of account = **$10**.
2. Position size = `$10 ÷ (stop distance × value per unit)`.
3. Round **down** to the broker's minimum increment.
4. **If rounding down gives zero — i.e. the minimum position risks more than $10 — skip the
   trade and log the reason.** This is the guard rail from the sizing analysis. It will fire on
   gold during volatile sessions, which is exactly when it should.
5. Maximum **3 open positions** at once.
6. **Correlation cap:** at most 2 positions sharing a dominant currency. Long EURUSD, long
   GBPUSD and short USDJPY is three ways of being short the dollar, not three trades.
7. **Daily stop:** after −3R ($30) on the day, no further signals until tomorrow.

*(Items 5–7 are proposals — say if you'd rather they were different.)*

### Exit management (every trade)
- **TP1 at 2R** → close 50% of the position, move the stop to breakeven.
- **TP2 at 3R** → close the remainder.
- **Hard time exit at 16:00 London.**
- Once the stop is at breakeven it does not move again. No trailing, no widening. A stop is
  never moved further from entry — that single rule prevents the most expensive mistake there is.

### Definitions used below
- **ATR** — Average True Range over 14 periods, a measure of how much the instrument typically
  moves. Used so stops adapt to conditions instead of being a fixed number of pips.
- **Swing high** — a bar whose high is higher than the 2 bars either side of it. Swing low is the
  mirror image. This is how "support and resistance" gets defined objectively rather than by eye.
- **R** — the distance from entry to stop. Every target is a multiple of it. A 2R win pays twice
  what a loss costs.

---

## Strategy 1 — London Open Breakout

**The idea:** markets go quiet during the Asian session, then move when London arrives. A tight
overnight range is stored-up energy; the break of it is the release.

**Instruments: EURUSD, GBPUSD, XAUUSD only.**
Not AUDUSD or USDJPY — those are *actively traded* during Asian hours, so their overnight range
isn't a quiet coil, it's a full trading session. The setup's logic doesn't apply to them.

**Rules:**

1. **Measure the overnight range.** Record the high and low from **00:00 to 06:00 London**.
2. **Compression filter.** Only trade if that range is **narrower than 75% of the median
   overnight range of the last 20 trading days**. A wide overnight range means the move already
   happened. This filter is doing real work — expect it to reject most days.
3. **Wait for London.** From **07:00**, place a resting stop order just beyond each side of the
   range: `range high + 0.1 × ATR` to buy, `range low − 0.1 × ATR` to sell. The buffer is there
   so a one-tick nick of the level doesn't trigger you.
4. **Entry** is whichever side triggers first. The opposite order is cancelled immediately.
5. **Stop-loss** goes at the *other* side of the range. So a break upward has its stop below the
   range low. R = the range height plus both buffers.
6. **Targets:** TP1 at 2R (half off, stop to breakeven), TP2 at 3R.
7. **One attempt per instrument per day.** If it triggers and stops out, that's the day. No
   re-entry — revenge-trading a failed breakout is how a $10 loss becomes a $40 one.
8. **Untriggered orders are cancelled at 11:00 London.** A breakout that hasn't happened by then
   isn't a London-open breakout any more.

**Where it fails:** false breakouts in choppy conditions. The compression filter and the news
blackout exist to reduce that, and the backtest will tell us by how much.

---

## Strategy 2 — Trend Pullback

**The idea:** don't chase a move — wait for it to pause and pull back, then join it at a better
price with a tighter stop. This is where your support/resistance, Fibonacci and candlestick
choices live.

**Instruments: all five.**

**Rules:**

1. **Establish the trend** on the 4-hour chart: price above the 50-period EMA is an uptrend,
   below is a downtrend. **Only take trades in that direction.**
2. **Confirm the trend is real,** not drifting sideways: ADX(14) on the 1-hour chart must be
   **above 20**. ADX measures trend strength regardless of direction; below 20 means "no trend
   worth following", and pullback strategies bleed money in those conditions.
3. **Identify the pullback zone.** Take the most recent completed 1-hour swing in the trend
   direction. The zone is where price retraces into either:
   - the **38.2%–61.8% Fibonacci retracement** of that swing, **or**
   - a **prior support/resistance level** — a swing high or low that price has reacted to at
     least twice before, within 0.5 × ATR of the current price.

   Both count. A zone where they overlap is flagged as higher conviction.
4. **Wait for the trigger.** Inside that zone, on the **15-minute chart**, require one of:
   - a **bullish engulfing** bar in an uptrend (or bearish engulfing in a downtrend) — a bar
     that completely covers the previous bar's range and closes in the trend direction; or
   - a **pin bar** — a bar with a wick at least twice the length of its body, poking into the
     zone and rejecting it.

   No trigger, no trade. Price entering the zone is not enough on its own — that's the
   difference between "buying the dip" and "catching a falling knife".
5. **Entry:** market order at the close of the trigger bar.
6. **Stop-loss:** `trigger bar's low − 0.25 × ATR` for a long (mirrored for a short), or below
   the swing low that formed the zone — **whichever is further away**. A stop placed too tight
   against structure is a donation.
7. **Targets:** TP1 at 2R (half off, stop to breakeven), TP2 at 3R.
8. **Maximum 2 attempts per instrument per day.**

**Where it fails:** trends that end exactly when you join them. The ADX filter and the 4h trend
direction are the defences, and neither is perfect.

---

## What I expect to happen

Being upfront so the results aren't a surprise:

- **At least one of these two will probably not survive costs.** That's a normal and useful
  outcome, not a failure of the project.
- **Gold will look best in the raw backtest and worst after the sizing guard rail** — its big
  ranges produce big R-multiples, but the minimum-lot rule will veto many of its trades.
- **The compression filter on Strategy 1 will cut the trade count hard** — possibly to 1–2 signals
  a week across three instruments. Few, good signals is the intent, but it also means it takes
  longer to accumulate enough trades to be confident about anything.
- **Sample size will be the binding constraint.** Fewer than ~100 trades and any conclusion is
  weak. This is why the strategies are simple: complex rules fire less often, and rare events
  can't be validated.
