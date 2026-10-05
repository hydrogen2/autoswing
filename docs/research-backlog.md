# Research backlog

Groomed by the owner in check-ins; the manager proposes ONE candidate per
Friday report. Every experiment must be measurement-first (shadow/ledger),
zero new capital risk, and answer a named decision.

## Active instruments

| # | Instrument | Started | Question it answers |
|---|---|---|---|
| 1 | Live PEAD ledger | 2026-07-09 | Does the drift edge pay? (verdict at 100 trades) |
| 2 | News-v2 shadow book | 2026-08-06 | Can catalyst momentum fill the earnings off-season? PRE-REGISTERED 2026-09-29 (at 12 closed, 2W/10L, -$927): verdict on the FIRST 25 closes — bin if avg alpha < 0 or total P&L < 0; otherwise a promotion discussion only if both hold with each one's best trade removed. Computed in code (`shadow-status` v2.preregistered_verdict); criteria closed to revision. |
| 3 | Forecast ledger (deep/quick tiers) | 2026-08-06 | Can the brain out-predict a coin on prints? Does research depth pay? |
| 4 | Exit counterfactuals | 2026-08-06 | Are our exit rules (2R/15d) leaving money on the table? |
| 5 | Skip ledger | 2026-08-06 | Does LLM judgment beat the raw scanner? |
| 6 | Regime tags in benchmark marks | 2026-08-06 | Does PEAD pay only in calm tapes? (analysis at ~50 trades) |
| 7 | Fill-quality baseline | 2026-08-31 | What does paper slippage translate to live? (28 fills: mean +44bps vs limit, paper-flattered) |
| 8 | Insider-buying enrichment | 2026-09-08 | Does Form 4 cluster buying predict drift? (measurement-only to ~100 candidates) |
| 9 | External-signal ledger | 2026-09-08 | Does following anyone's disclosed buys beat their own benchmark? (sources: insider Form 4, congress, hand-logged accounts) |
| 11 | Completed-session entry counterfactual | 2026-10-05 | Should "wait one full session that holds the gains" be a hard entry rule? PRE-REGISTERED bar (a678753, before the first run): ex-KMX/SNPS, replay beats live on dollars AND R, gain survives dropping the most helpful trade, n>=20. RESULT 10-05 (n=33): PASS — live +$204/+0.21R vs replay +$1,033/+1.65R; gain +$830, +$330 without the best trade. It wins by sitting out 24 of 33 trades (avoids 15 losers worth $4,205, misses 9 winners worth $3,790, incl. 5 of the 6 biggest). One quarter, in-sample: a case for the owner, not an automatic change. `entry-counterfactual`. OUT-OF-SAMPLE 10-05 (2023-25 backtest, bar pre-registered caa9d27): `better_per_trade_only` — confirm n=689 avg +0.158R / total +108.8R vs skeleton n=1612 +0.055R / +87.9R, but total R higher in only 1 of 3 years. NOT a hard-rule case; stays judgment. The delay-only variant (+161.3R on 2023-25, unregistered) was then pre-registered (530df7c) and run on UNSEEN 2021-22: `no_support` — delay n=553 +0.022R/+12.4R vs skeleton n=690 +0.079R/+54.2R; confirm also worse there. CLOSED 10-05: neither variant becomes a hard rule; stays judgment. |
| 10 | Wheel book (cash-secured puts) | 2026-09-24 | Does selling puts on names we would own beat simply owning them? (verdict at 40 closed cycles) |

## Queued (not started)

- **Forecast monetization (v3 options overlay)** — defined-risk structures
  on high-confidence forecasts. Still blocked on: forecast deep tier showing
  calibrated edge at n>=100; owner decision; IBKR options permission.
  CORRECTION 2026-09-24: the "paid data" half of this blocker was wrong.
  yfinance serves full live chains (strike/bid/ask/IV/volume/OI) for free,
  which is enough to SCREEN and to measure forward. What it does not serve
  is option HISTORY, so nothing in options can be backtested here — and
  approximating past premiums from realized vol would zero out the
  implied-minus-realized gap that is the entire edge. Options work in this
  repo is therefore forward-measured or not done at all.
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

- Tweet-parsing a specific account (Serenity / @aleabitoreddit) — REJECTED
  2026-09-08 as its own book. Unstructured input, entries announced loudly
  while exits go quiet (so any parser inherits a hold-the-losers bias), and
  one public test already found an equal-weight portfolio of his calls
  underperformed SPY and XLK. If we want him measured, hand-log his calls
  into the external-signal ledger — measurement does not require automation,
  and a parser is only worth building for a signal that has already shown
  something.

- Prediction-market venues (Polymarket etc.) — illegal to access from
  Singapore; unverifiable claims; wrong venue class for this project
  (2026-08-06).
