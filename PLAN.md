# Autoswing — Design Plan

*Converged 2026-07-07 after discussion. No code exists yet; this document is the contract for what gets built.*

## 1. Mission

An always-on, calm, pattern-literate **scout** that explores the opportunity space the owner's long-term portfolio excludes by design:

1. **Gray zone** — act on sub-conviction signals (e.g. a quality name dips hard on no thesis change) with small, stop-protected, time-boxed swing entries.
2. **Other universes** — hunt outside the owner's AI/growth holdings (healthcare, energy, industrials, financials). Post-earnings drift is stronger in less-followed names, and the bot has no familiarity bias.
3. **Always-on watcher** — the honest edge is **coverage + timeliness + discipline-at-scale**: awake for every pre-market print, reads every report in the universe, applies the playbook identically in week 1 and week 60. The edge is *not* out-analyzing Wall Street.

**Priority #1 is capital growth.** Full compounding, no profit sweep. Cash withdrawals happen only when the owner asks in advance; the bot plans an orderly exit.

## 2. Targets and kill criteria

| Item | Value |
|---|---|
| Starting capital | $50,000 (IBKR paper account) — mirrors realistic live funding; chosen over $10k for faster evidence (more concurrent positions → more closed trades per season) and a smoother, more representative equity curve |
| Return target | ~13–15% annualized, gross |
| Max drawdown | **15% from high-water mark → kill-switch halts all trading**; human must review and reset |
| Benchmark | VOO buy-and-hold, tracked from day one in the journal |
| Success bar | Beat the benchmark **risk-adjusted** (return per unit of drawdown) over the paper period |
| Kill criteria | After 6–12 months of paper, if not clearly beating the index risk-adjusted: fix the edge or shut down. Pre-committed, no falling in love. |
| Scaling gate | Add capital only after ≥100 closed trades AND ≥6 months AND survived at least one bad stretch within limits. Never on a hot streak. |

## 3. Strategy v1: Post-Earnings Announcement Drift (PEAD)

Positive-skew by construction: many small losses, occasional large winners.

- **Signal**: company reports earnings; look for a meaningful surprise (beat/miss vs. consensus, guidance direction) confirmed by the price reaction (gap + volume) on day 1.
- **Entry**: *after* the print, once the gap is known — never hold a position through an earnings report (binary gap risk defeats stops).
- **Direction**: long on positive surprise + confirming reaction; short (or skip, in v1 possibly long-only) on negative.
- **Exit**: cut losers fast at the stop; let winners run with a trailing stop; hard time-box (e.g. max 15 trading days) so capital recycles.
- **The LLM's job**: judgment on the ambiguous middle — is the drift intact or exhausted? was the "beat" low quality (one-time items)? is the reaction already overdone? — *not* mechanical signal generation, and *never* risk enforcement.

**v2 (later, same framework)**: news/catalyst momentum. The framework below is strategy-agnostic; only the candidate-selection brain changes.

## 4. Architecture

**Core principle: the LLM proposes; deterministic code disposes.**

```
┌──────────────────────────────────────────────┐
│ Scheduled Claude Code agent (the "brain")     │
│ wakes pre-market + intraday checkpoints       │
└───────────────┬───────────────────────────────┘
                │ tool calls (local MCP / CLI)
   ┌────────────┼───────────────┬───────────────┐
   ▼            ▼               ▼               ▼
 Market data  Earnings/news   RISK GATE       Broker layer
 (delayed OK) (catalysts)     (pure Python,   (ib_async →
                               no LLM)         IB Gateway)
                                  │
                                  ▼
                        IBKR paper acct (port 4002)
                        [live = port 4001, later]
```

### Components

- **Broker layer** — IB Gateway (headless) in a container; `ib_async` (maintained successor of `ib_insync`). Exposes a minimal tool surface: `get_account`, `get_positions`, `get_quote`, `place_bracket_order`, `cancel_order`, `flatten_all`. **Bracket orders only** (entry + stop + target attached atomically) so no position is ever naked, even if the agent crashes mid-run.
- **Risk gate** — pure Python, hard-coded limits (section 5). Validates every proposed order; logs every accept/reject with reason. The LLM cannot bypass or reconfigure it.
- **Data layer** (free tier for paper):
  - **SEC EDGAR** — free, official, authoritative filings (8-K earnings releases).
  - **yfinance** — prices, history, basic estimates (unofficial; acceptable for paper).
  - **Finnhub free tier** — earnings calendar (60 calls/min).
  - **IBKR** — delayed quotes (fine for swing horizons) + included news headlines.
  - Upgrade to a paid feed (~$30–100/mo: FMP/Polygon/Finnhub paid) **before** going live.
- **Brain** — a scheduled Claude Code routine. Each run: pull account + positions + candidates + fresh news → reason → emit **structured trade proposals (JSON)** with ticker, direction, size, stop, target, time-box, and a written rationale → gate → broker.
- **Journal** — append-only log of every proposal, gate decision, order, fill, and a daily equity mark vs. VOO. This is the audit trail, the performance scoreboard, and doubles as the owner's daily "what's moving outside AI" digest.

### Daily schedule (US/Eastern, trading days)

| Time | Run | Job |
|---|---|---|
| ~08:00 | Pre-market scan | Ingest last-night/this-morning earnings; shortlist candidates; check overnight news on open positions |
| ~10:00 | Entry window | Confirm reactions post-open (skip first chaotic minutes); place new bracket orders |
| ~12:30 | Midday check | Manage positions; tighten/trail stops; react to material news |
| ~15:30 | Pre-close | Exits for time-boxed positions; **exit anything reporting earnings after close**; end-of-day journal + benchmark mark |

## 5. Risk gate (hard rules, not suggestions)

Scaled to $50k; all values live in one config file, changeable only by the human.

- **Risk per trade**: 1% of equity (~$500) — position size derived from stop distance.
- **Max position size**: 15% of equity (~$7,500 notional).
- **Max open positions**: 10.
- **Max gross exposure**: 100% of equity (no leverage in v1).
- **Stop-loss required** on every order — proposals without one are rejected.
- **No holding through earnings**: gate rejects entries within N days before a position's next report; scheduler force-exits before prints.
- **Daily loss kill-switch**: −3% realized+unrealized in a day → flatten nothing automatically, but no *new* entries until next day.
- **Max drawdown kill-switch**: −15% from equity high-water mark → halt all trading; human review required to resume.
- **Core-overlap cap**: max 1 open position overlapping the owner's long-term holdings, and the journal must flag it ("you now hold X in both books"). Gray-zone trades on core names are allowed — that's part of the mission — but capped and disclosed.
- **Sanity checks**: ticker exists and is liquid (min avg dollar volume), market is open, duplicate-order suppression, PDT guard (block >3 same-day round trips per 5 days — dormant above $25k equity, auto-activates if equity ever falls below).

## 6. Rollout phases

1. **Phase 0 — Plumbing.** IB Gateway + paper account connected; broker tool surface working; journal writing; place/cancel a test bracket order.
2. **Phase 1 — Risk gate.** Implement + unit-test every rule above, including adversarial cases (absurd size, fake ticker, missing stop).
3. **Phase 2 — Data.** Earnings calendar + surprise data + price reaction pipeline producing a clean daily candidate list.
4. **Phase 3 — Brain, fully auto on paper.** Scheduled runs, structured proposals, no human approval (per owner's choice). Let it run full earnings cycles.
5. **Phase 4 — Evaluate.** 6–12 months paper vs. VOO, risk-adjusted. Apply kill criteria honestly.
6. **Phase 5 — Live, tiny.** Same code, port flag flipped, small real capital. Only after Phase 4 passes. **Hard blockers before the flag flips**: paid data feed; the orphan-order reconciler (src/autoswing/reconcile.py, built 2026-07-15 after the docs/incidents/2026-07-14 incident, running hourly in SHADOW mode) promoted to enforce only after ≥4 weeks with zero false-positive decisions; account admin (resets, transfers) only ever on a flat book.

## 7. Known constraints & honest caveats

- **Taxes**: all gains are short-term (ordinary income). The live bar is therefore higher than the paper bar; factored into kill criteria.
- **Free data is imperfect**: delayed, occasionally stale/wrong. Acceptable for paper; a listed upgrade blocker for live.
- **A raging AI bull market will likely beat this bot.** That's fine — its job is a *different return stream* plus exploration, not beating the owner's growth portfolio at its own game.
- **The paper→live gap is real**: paper fills are optimistic (no slippage). Expect live results a bit worse; size expectations accordingly.
