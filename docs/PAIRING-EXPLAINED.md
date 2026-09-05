# "Pairing": the two things it can mean

You weren't sure which you meant, so here's both — in plain English, with worked examples on
your actual instruments (FX majors + gold/silver). All numbers below are **illustrative, made
up to show the mechanics** — they are not from real data.

---

## Meaning 1: Directional trading (one instrument at a time)

This is what most people mean by "day trading". You pick one instrument, decide it's going up
or down, and place a trade with a stop and a target.

### Worked example — London Open Breakout on gold

Gold typically drifts quietly during the Asian session, then moves when London opens. The rule:

1. **Measure the Asian range.** From 00:00–06:00 UK time, record gold's high and low.
   Say: high $2,412.00, low $2,404.00. Range = $8.
2. **Wait for London.** At 07:00 UK (14:00 or 15:00 your time), watch for price to break out.
3. **Entry.** Price breaks above $2,412.00 → buy at **$2,412.50**.
4. **Stop-loss.** Placed below the range, sized by recent volatility.
   Say ATR is $6, and we use 1.0× ATR → **stop at $2,406.50**. Risk = $6.00 per unit. This is
   your **1R**.
5. **Take-profit.** Expressed as multiples of that risk:
   - TP1 at **2R** = $2,412.50 + $12.00 = **$2,424.50** (close half, move stop to breakeven)
   - TP2 at **3R** = **$2,430.50** (close the rest)
6. **Position size.** With a £5,000 account risking 1% (£50) and a $6.00 stop, you'd size the
   trade so a $6.00 adverse move costs you £50 — roughly 0.08 lots on a standard gold contract
   (illustrative; the real calculation depends on contract size and GBP/USD).

**What can go wrong:** false breakouts. Price pokes above $2,412, triggers your buy, then
collapses back into the range and stops you out. This is the single most common failure of
breakout systems, and the reason a *filter* matters — e.g. only take the breakout if the Asian
range was unusually tight, or if there's no high-impact news in the next 30 minutes.

**What the tool would do:** compute the range each morning, watch for the break, and Telegram
you: *"XAUUSD long, entry 2412.50, SL 2406.50, TP1 2424.50, TP2 2430.50, size 0.08 lots, risk
£50. Rule: London breakout. Historical: 41% win rate, +0.28R expectancy over 340 trades."*

### Other directional setups worth testing
- **Trend-pullback** — if the 4-hour trend is up, buy dips to a moving average on the 15-minute chart.
- **VWAP reversion** — when the market is ranging, fade moves that stretch too far from the day's average price.

---

## Meaning 2: Statistical pairs trading (two instruments, betting on the *relationship*)

Here you don't care whether gold goes up or down. You care whether gold and silver have drifted
apart from their normal relationship — and you bet they'll snap back together.

### Worked example — the gold/silver ratio

Gold and silver move together most of the time. Their ratio (gold price ÷ silver price) has a
typical range. Suppose over the last 60 days the ratio averaged **82.0**, and it usually stays
within ±1.5 of that.

**Monday:** gold rallies hard, silver doesn't follow. Ratio hits **85.0** — unusually stretched.

The trade: **sell gold and buy silver simultaneously**, in sizes chosen so the two legs have
roughly equal dollar exposure. You are not betting on the metals complex going up or down. You
are betting the gap between them narrows.

- **Entry:** when the ratio is ~2 standard deviations from its mean (here, 85.0).
- **Exit (take-profit):** when the ratio returns to its mean (82.0).
- **Stop-loss:** when the ratio stretches *further*, to ~3 standard deviations (say 86.5) — i.e.
  the relationship you were betting on has broken.

**Why this is attractive:** if the whole metals market crashes 3%, both legs lose and gain
roughly together, and you're largely unharmed. It's insulated from the "everything moved because
of the Fed" risk that kills directional trades.

**Why it's harder:**
- You need to establish the relationship is *statistically real*, not a coincidence. The formal
  test is **cointegration** (the standard reference is Engle–Granger, 1987 — high confidence).
  Correlation alone is not enough and misleads badly.
- **You pay costs on two legs, not one.** Two spreads in, two spreads out. On CFDs that's a
  meaningful drag, and it's why many published pairs strategies stop working when you add real
  retail costs.
- **The relationship can genuinely break.** In 2020 the gold/silver ratio went to extremes far
  outside any historical band. A mean-reversion trade against that would have been badly hurt.
  This is why the stop-loss rule matters and why position sizing must assume it can happen.
- It's less intuitive to watch. You can't look at a chart and feel whether it's working.

### Candidate pairs in your universe
| Pair | Why they move together |
| --- | --- |
| **XAUUSD / XAGUSD** | Both precious metals, same macro drivers. The classic. |
| **AUDUSD / NZDUSD** | Neighbouring economies, similar commodity exposure. Tightest FX relationship among the majors. |
| **EURUSD / GBPUSD** | Both European, both vs USD. Looser, but tradeable. |
| **USDCAD / oil** | Canada exports oil; CAD strengthens when oil rises. Needs an oil instrument. |
| **EURUSD × USDJPY vs EURJPY** | A triangular relationship. True arbitrage here is long gone to HFT, but slower statistical deviations exist. |

---

## Side by side

| | **Directional** | **Statistical pairs** |
| --- | --- | --- |
| What you bet on | Price goes up or down | Two prices converge |
| Instruments per trade | 1 | 2 |
| Trading costs | 1 spread each way | 2 spreads each way |
| Hurt by a market-wide shock | Yes, badly | Mostly no |
| Intuitive to follow | Yes — you can see it on a chart | No — needs a computed spread chart |
| Signals per week (typical) | More | Fewer; it waits for divergence |
| Complexity to build | Moderate | Higher — needs cointegration testing, hedge ratios, rolling recalibration |
| Where it fails | False breakouts, choppy regimes | The relationship permanently breaks |

---

## My recommendation for you

**Start directional, add pairs as phase 2.** Reasons:

1. You said you don't have a setup you already trade. Directional signals are far easier to
   *judge* — when the tool says "buy gold at 2412.50", you can look at a chart and see whether
   that made sense. That feedback loop is how you learn whether to trust it. A pairs signal is
   opaque until you've built intuition for the spread.
2. It gets you a working end-to-end system faster: data → signal → Telegram alert → journal.
   Every piece of that infrastructure is reused by the pairs strategy later, so nothing is wasted.
3. The gold/silver pair is genuinely interesting and I'd like to build it — but it deserves to be
   built on a harness that has already been proven correct on the simpler case. If pairs are the
   first thing we build and the backtest looks amazing, neither of us will know whether that's an
   edge or a bug.

Concretely: **phase 1** = London-open breakout + trend-pullback on EURUSD, GBPUSD, XAUUSD.
**Phase 2** = the XAUUSD/XAGUSD and AUDUSD/NZDUSD spreads.
