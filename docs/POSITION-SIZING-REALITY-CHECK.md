# Position sizing at $1,000 / 1% — what actually fits

You've set a **$1,000 USD account risking 1% ($10) per trade**. That's a sensible, disciplined
choice. But it collides with a hard constraint that has to be dealt with now rather than
discovered later: **brokers have minimum position sizes**, and on some of your chosen
instruments the minimum already risks more than $10.

Contract specifications vary between brokers (moderate confidence on the exact figures below —
they're the common retail conventions, and must be verified against whichever broker you pick).
The standard assumptions used here:

| Instrument | 1.00 lot | Smallest step (typical) | Value of smallest step |
| --- | --- | --- | --- |
| EURUSD / GBPUSD / AUDUSD | 100,000 units | 0.01 lot | $0.10 per pip |
| USDJPY | 100,000 units | 0.01 lot | ~$0.067 per pip (varies with the rate) |
| XAUUSD | 100 oz | 0.01 lot = 1 oz | $1.00 per $1.00 move |
| XAGUSD | 5,000 oz | 0.01 lot = 50 oz | $0.50 per $0.01 move |

## Working it through

Position size = `$10 risk ÷ (stop distance × value per unit of movement)`.

| Instrument | Typical intraday stop | Size needed for $10 risk | Broker minimum | Verdict |
| --- | --- | --- | --- | --- |
| **EURUSD** | 20 pips | 0.05 lot | 0.01 | ✅ Comfortable |
| **GBPUSD** | 25 pips | 0.04 lot | 0.01 | ✅ Comfortable |
| **AUDUSD** | 20 pips | 0.05 lot | 0.01 | ✅ Comfortable |
| **USDJPY** | 25 pips | ~0.06 lot | 0.01 | ✅ Comfortable |
| **XAUUSD** | $6.00 | 0.0167 lot | 0.01 | ⚠️ Rounds to 0.01 ($6 risk) or 0.02 ($12 risk) |
| **XAUUSD** | $12.00 (busier day) | 0.0083 lot | 0.01 | ❌ Below minimum — forced to risk $12 (1.2%) |
| **XAGUSD** | $0.30 | 0.0067 lot | 0.01 | ❌ Below minimum — forced to risk $15 (1.5%) |

### What this means

- **The four FX majors are fine.** Sizes land in a range where 0.01-lot granularity is a rounding
  detail, not a constraint. Good.
- **Gold is workable but coarse.** On calm days it fits. On volatile days — exactly the days a
  breakout system wants to trade — the ATR-based stop widens past $10 and the minimum position
  forces you above your risk limit. You'd be taking 1.2%+ trades precisely when conditions are
  most dangerous. That's backwards.
- **Silver does not fit at all.** At the standard 5,000 oz contract, one minimum-size position
  risks around $15 on a normal stop. There is no way to trade XAGUSD within a $10 budget.

## Four ways to resolve it

**Option A — Drop silver, keep gold with a guard rail.**
Trade the four FX majors plus XAUUSD. Add a rule to the risk layer: *if the minimum position
size would risk more than the budget, skip the trade and log why.* On quiet-gold days you trade;
on wild days you sit out. Honest, safe, and costs nothing. The trade-off: it removes the
gold/silver spread from phase 2, since that needs both legs.

**Option B — Find a broker with nano lots or a cent account.**
Some brokers offer 0.001-lot minimums, and "cent accounts" denominate the balance in cents so a
$1,000 deposit trades as though it were $100,000 with proportionally tiny positions. Either
solves the granularity problem completely and keeps silver in play. Moderate confidence that
suitable options exist for an Australian-resident retail client — this needs checking against
ASIC-regulated brokers specifically, and cent accounts are more common at offshore entities,
which is a real counterparty-risk trade-off worth weighing.

**Option C — Raise risk per trade to 2%.**
$20 per trade makes gold comfortable and silver marginal. I'd push back on this: 2% per trade
means a 6-trade losing streak — entirely normal — costs 12% of the account, and it's the most
common way small accounts get wound down. The constraint is telling you something true, and
loosening it to make the constraint go away is the wrong direction.

**Option D — Start with a larger account.**
$3,000–5,000 at 1% makes everything fit cleanly. Only you know if that's realistic, and it is
absolutely not a prerequisite — Option A works fine at $1,000.

## Recommendation

**Option A now, revisit B when you choose a broker.** Build for the four FX majors + XAUUSD, with
the "minimum size exceeds risk budget → skip and log" rule in the sizing layer from day one.
Keep XAGUSD in the backtest universe (it costs nothing to include in research) but mark it
non-tradeable at current account size. If you later open an account with finer granularity, it
becomes tradeable with a config change and no code change.

## One more thing worth saying plainly

You've said real money is the goal, so this matters: at $1,000 risking $10 a trade, a genuinely
good system returning +0.3R per trade across ~150 trades a year makes about **$450**. That is a
real 45% return and would be an excellent result — and it is also $450. The account at this size
is a **learning and validation vehicle**, not an income source.

That's not a discouragement; it's the correct way to use it. It means:
- Any recurring cost (a VPS, a data subscription) is significant against the P&L. Stay at £0.
- The thing being tested is *the system and your execution discipline*, not the money.
- Scaling up is a decision to make **after** a validated live track record — and the mistake
  almost everyone makes is skipping that step because the small-account returns feel too slow.
