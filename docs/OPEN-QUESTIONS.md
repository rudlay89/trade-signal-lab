# Open questions

Answers here drive the build. Marked ★ where the answer materially changes the architecture.

> **Answered so far:** 1, 2, 5, 6, 7, 8, 14, 15, 16, 18, 22, 23, 26 — see
> [`DECISIONS.md`](DECISIONS.md) for the answers and what they changed. The questions below are
> kept in full for reference; ✅ marks the ones now settled.

## A. Purpose & scope

1. ✅ ★ **What does the tool actually hand you?** A ranked shortlist you review and trade
   manually, alerts as setups appear, or fully automated order placement?
2. ✅ ★ **Signals only, or execution too?** Execution roughly doubles the scope and adds the
   entire failure domain of order management, partial fills and reconnection logic.
3. Is this primarily for **learning/research**, or is real capital going in? If real: over what
   horizon, and what would make you stop?
4. Who uses it — just you, or would you want to share signals with others? (Sharing signals for
   money is a regulated activity in most jurisdictions; worth knowing early.)

## B. Instruments & timeframes

5. ✅ ★ **Which instruments, concretely?** e.g. FX majors only; + XAUUSD/XAGUSD; + indices
   (US30, NAS100, DAX); + crypto; + oil. Fewer instruments = better validation per instrument.
6. ✅ ★ **What holding period?** Scalping (minutes), intraday (hours, flat by session end), or
   swing (days)? This determines data granularity, cost sensitivity, and whether swap matters.
7. ✅ **Which sessions can you actually trade or monitor?** Asian / London / NY / overlap?
   What timezone are you in, and are you at a screen during those hours?
8. ✅ **"Pairing" — which did you mean?**
   (a) currency pairs generally, (b) statistical pairs/spread trading like gold-vs-silver or
   AUDUSD-vs-NZDUSD, or (c) both?

## C. Risk

9. ★ **Account size and risk per trade?** (e.g. 0.5–1% per trade is the common convention.)
10. **Max concurrent positions**, and do you want correlation-aware exposure caps?
11. **Daily / weekly loss limits** that halt signal generation?
12. Fixed R targets, trailing stops, partial take-profits, or a mix?
13. **Are you trading a prop-firm account?** If so, its drawdown rules (daily loss, trailing
    max DD, consistency rules) become hard constraints the sizing layer must respect.

## D. Data & broker

14. ✅ ★ **Do you already have a broker?** Which one, and does it offer an API?
15. ✅ **Budget for data/infrastructure** — £0 (free sources only), ~£20–50/mo, or more?
16. ✅ Do you need a **live feed**, or is end-of-session analysis with next-day planning enough?
17. Do you want an **economic calendar** integration to blackout high-impact news?

## E. Technical

18. ✅ ★ **Language preference** — Python is my strong recommendation for the ecosystem. Any
    reason to prefer something else?
19. **Where does it run?** Your laptop on demand, or an always-on VPS firing during London/NY?
20. ✅ **Interface** — CLI + daily Markdown/HTML report, a web dashboard, Telegram alerts, or
    something written into a spreadsheet?
21. **Repository placement** — a folder in `My-Personal-projects` (current assumption, matching
    `poo-tracker`), or a separate standalone GitHub repo?
22. ✅ What's your Python/programming comfort level? It changes how much scaffolding vs.
    explanation is worth building.

## F. Strategy

23. ✅ **Do you already trade a setup you'd like encoded?** Formalising a strategy you understand
    beats inventing one from scratch — you can sanity-check its output.
24. Any indicators or concepts you specifically want (ICT/order blocks, supply-demand,
    Fibonacci, Elliott, VWAP, market profile)?
25. Are you open to being told a strategy doesn't work once backtested, or should the tool
    produce signals regardless and let you judge?
26. ✅ Appetite for machine learning, or rules-only to start? *(rules-only to start)*

## G. Success criteria

27. **What does "this tool works" look like to you in 3 months?** A validated backtest? A
    profitable paper-trading month? Simply a disciplined daily routine?
28. What's the failure mode you'd most regret — missing good trades, or taking bad ones?
