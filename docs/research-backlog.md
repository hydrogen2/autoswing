# Research backlog

Groomed by the owner in check-ins; the manager proposes ONE candidate per
Friday report. Every experiment must be measurement-first (shadow/ledger),
zero new capital risk, and answer a named decision.

## Active instruments

| # | Instrument | Started | Question it answers |
|---|---|---|---|
| 1 | Live PEAD ledger | 2026-07-09 | Does the drift edge pay? (verdict at 100 trades) |
| 2 | News-v2 shadow book | 2026-08-06 | Can catalyst momentum fill the earnings off-season? |
| 3 | Forecast ledger (deep/quick tiers) | 2026-08-06 | Can the brain out-predict a coin on prints? Does research depth pay? |
| 4 | Exit counterfactuals | 2026-08-06 | Are our exit rules (2R/15d) leaving money on the table? |
| 5 | Skip ledger | 2026-08-06 | Does LLM judgment beat the raw scanner? |
| 6 | Regime tags in benchmark marks | 2026-08-06 | Does PEAD pay only in calm tapes? (analysis at ~50 trades) |
| 7 | Fill-quality baseline | 2026-08-31 | What does paper slippage translate to live? (28 fills: mean +44bps vs limit, paper-flattered) |
| 8 | Insider-buying enrichment | 2026-09-08 | Does Form 4 cluster buying predict drift? (measurement-only to ~100 candidates) |

## Queued (not started)

- **Forecast monetization (v3 options overlay)** — defined-risk structures
  on high-confidence forecasts. HARD-BLOCKED on: forecast deep tier showing
  calibrated edge at n>=100; owner decision; options approval + paid data.
- **Earnings-call tone as structured field** — guidance direction /
  one-time-items flags logged per candidate instead of freeform rationale;
  regress drift against them at ~100 candidates.

## Completed

- **Fill-quality analysis** — SHIPPED 2026-08-31 (e7f1f63), allowlisted for
  the headless manager 09-02 (985898c). Baseline: 28 entry fills, mean
  +44.2 bps improvement vs the approved limit, worst case 0.0 bps. Read it
  as an upper bound — paper fills flatter, and the point of the instrument
  is to calibrate how much live trading erodes it.
- **Corroborated-cancel promotion** — DONE 2026-09-02 (3b1129f). Promoted on
  439 shadow runs / 35 days: only 3 decisions ever produced, all the VOYG
  08-06 near-miss, then 18 days of zero decisions while correctly declining
  9 pending entries and 2 transient suspects. The clean record could not
  prove the ACTION path worked (it had never run and had no test), so
  tests/test_reconcile_enforce.py was written first — mutation-checked.
  Generalisable: a shadow record proves a component does not FIRE wrongly,
  never that firing WORKS. Test the action path separately.
- **Gate holiday-awareness** — SHIPPED 2026-09-08 after Labor Day exposed a
  clock-only market_hours rule that passed all session on a closed market.
  Static NYSE table (autoswing/calendar.py) covering full closures AND
  early closes, failing CLOSED past its coverage date. Not strictly a
  research item, but it came out of the same review loop.

## Retired / rejected

- Prediction-market venues (Polymarket etc.) — illegal to access from
  Singapore; unverifiable claims; wrong venue class for this project
  (2026-08-06).
