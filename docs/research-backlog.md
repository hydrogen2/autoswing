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

## Queued (not started)

- **Insider-buying enrichment** — SEC Form 4 cluster buys as a scanner
  field; measure correlation with drift before it influences decisions.
  Cost: EDGAR parser. Blocked on: nothing; next in line.
- **Fill-quality analysis** — paper fills vs quoted spread, from existing
  journal data (gate.decision entry_limit vs broker.recent_fills price).
  Needed before go-live to translate paper results into live expectations.
  Analysis-only; data already collected.
- **Forecast monetization (v3 options overlay)** — defined-risk structures
  on high-confidence forecasts. HARD-BLOCKED on: forecast deep tier showing
  calibrated edge at n>=100; owner decision; options approval + paid data.
- **Corroborated-cancel promotion** — reconciler shadow→enforce. Gated on
  ≥4 clean weeks (see PLAN.md go-live blockers); check the shadow record.
- **Earnings-call tone as structured field** — guidance direction /
  one-time-items flags logged per candidate instead of freeform rationale;
  regress drift against them at ~100 candidates.

## Retired / rejected

- Prediction-market venues (Polymarket etc.) — illegal to access from
  Singapore; unverifiable claims; wrong venue class for this project
  (2026-08-06).
