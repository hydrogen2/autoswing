# Research backlog

Groomed by the owner in check-ins; the manager proposes ONE candidate per
Friday report. Every experiment must be measurement-first (shadow/ledger),
zero new capital risk, and answer a named decision.

## Active instruments

| # | Instrument | Started | Question it answers |
|---|---|---|---|
| 1 | Live PEAD ledger | 2026-07-09 | Does the drift edge pay? (verdict at 100 trades) |
| 2 | News-v2 shadow book | 2026-08-06 | Can catalyst momentum fill the earnings off-season? PRE-REGISTERED 2026-09-29 (at 12 closed, 2W/10L, -$927): verdict on the FIRST 25 closes — bin if avg alpha < 0 or total P&L < 0; otherwise a promotion discussion only if both hold with each one's best trade removed. Computed in code (`shadow-status` v2.preregistered_verdict); criteria closed to revision. LANDED 2026-11-02: `no_promotion_rests_on_one_trade` (+$228.49, avg alpha +0.13%; without the best trade -$592.17 / -0.576%). Owner ruled 11-04: KEEP the book as a new out-of-sample period. OUT-OF-SAMPLE PRE-REGISTERED 2026-11-06 (88e36bd), owner-ratified 2026-11-10 at 2/25 (CRL -$330.54, HHH +$219.09): verdict at the first 25 closes after the frozen sample, same bin/promotion criteria, exits = the shadow-mark code path only (stop/target/15d timebox/pre-earnings exit 2 days before a gate-verified print; no hand scoring). Computed in code (`shadow-status` v2.oos_preregistered_verdict); closed to revision. The owner is emailed once when it lands; retiring the book stays owner-only. |
| 3 | Forecast ledger (deep/quick tiers) | 2026-08-06 | Can the brain out-predict a coin on prints? Does research depth pay? PRE-REGISTERED 2026-10-19 (at deep n=76 65.8% vs quick n=166 69.3%; deep's reads ran 73.1 → 69.8 → 66.7 → 65.8): verdict at the FIRST 100 deep EPS scores, quick comparator = every quick EPS score dated at or before the 100th deep score; `depth_premium` only if deep beats quick by >= 3pp (the 09-18 premium), else `no_depth_premium` — a discussion trigger on whether deep pre-print research earns its run cost; quick tier unaffected. Computed in code (`forecast-stats` depth_verdict); closed to revision. LANDED 2026-11-04: `no_depth_premium` — deep 69.0% vs quick 68.5% at n=100 (+0.5pp vs the 3pp bar). CLOSED 11-05: owner retired the deep tier; forecast experiment continues quick-only (EPS calls ~69% both tiers, well above chance). |
| 4 | Exit counterfactuals | 2026-08-06 | Are our exit rules (2R/15d) leaving money on the table? |
| 5 | Skip ledger | 2026-08-06 | Does LLM judgment beat the raw scanner? |
| 6 | Regime tags in benchmark marks | 2026-08-06 | Does PEAD pay only in calm tapes? (analysis at ~50 trades) |
| 7 | Fill-quality baseline | 2026-08-31 | What does paper slippage translate to live? (28 fills: mean +44bps vs limit, paper-flattered) |
| 8 | Insider-buying enrichment | 2026-09-08 | Does Form 4 cluster buying predict drift? (measurement-only to ~100 candidates) PRE-REGISTERED 2026-10-23, owner-approved 2026-10-27, BEFORE the first read: verdict at n>=100 measured unique (symbol, day) rows; `cluster_premium` only if cluster-positive mean fwd-15d beats non-cluster by >= 2pp AND the gap still clears 2pp after dropping cluster's single best symbol-day; else `no_cluster_premium` and the enrichment comes out of the scan. Measured = 15 full sessions elapsed. Computed in code (`insider-outcomes` preregistered_verdict, c46cbc2); closed to revision. | READ #1 10-30: `no_cluster_premium` — cluster -17.9% (n=13) vs non-cluster +4.6% (n=98) fwd-15d, survives best-drop. CLOSED 11-04: enrichment removed from the live scan per the rule (owner ruling); the inverse 'cluster_avoid' pre-registration was BINNED (n=13, a sell signal found by looking at the same rows). |
| 9 | External-signal ledger | 2026-09-08 | Does following anyone's disclosed buys beat their own benchmark? (sources: insider Form 4, congress, hand-logged accounts) |
| 12 | Wide-ledger pre-registered verdict | 2026-10-14 | Does the wide book beat live on the SAME trades? First 40 wide closes, paired with live realized exits (live rescaled to $5k notional); playbook discussion only if wide wins on total dollars AND avg R and both margins survive dropping wide's best trade. Computed in `shadow-status` wide.preregistered_verdict; at registration 23 closed (19 paired: wide -$788 vs live -$392, avg R 0.006 vs 0.013; 4 unpaired +$1,661). Closed to revision. |
| 11 | Completed-session entry counterfactual | 2026-10-05 | Should "wait one full session that holds the gains" be a hard entry rule? PRE-REGISTERED bar (a678753, before the first run): ex-KMX/SNPS, replay beats live on dollars AND R, gain survives dropping the most helpful trade, n>=20. RESULT 10-05 (n=33): PASS — live +$204/+0.21R vs replay +$1,033/+1.65R; gain +$830, +$330 without the best trade. It wins by sitting out 24 of 33 trades (avoids 15 losers worth $4,205, misses 9 winners worth $3,790, incl. 5 of the 6 biggest). One quarter, in-sample: a case for the owner, not an automatic change. `entry-counterfactual`. OUT-OF-SAMPLE 10-05 (2023-25 backtest, bar pre-registered caa9d27): `better_per_trade_only` — confirm n=689 avg +0.158R / total +108.8R vs skeleton n=1612 +0.055R / +87.9R, but total R higher in only 1 of 3 years. NOT a hard-rule case; stays judgment. The delay-only variant (+161.3R on 2023-25, unregistered) was then pre-registered (530df7c) and run on UNSEEN 2021-22: `no_support` — delay n=553 +0.022R/+12.4R vs skeleton n=690 +0.079R/+54.2R; confirm also worse there. CLOSED 10-05: neither variant becomes a hard rule; stays judgment. |
| 10 | Wheel book (cash-secured puts) | 2026-09-24 | Does selling puts on names we would own beat simply owning them? (verdict at 40 closed cycles) |
| 13 | Short-side skeleton replay | 2026-11-10 | Does the drift exist on earnings MISSES, i.e. is there a short leg worth shadowing? Owner-initiated ("long-short pairs -- can we try these?"), run by the owner session, not a manager proposal. PRE-REGISTERED 2026-11-10 in code BEFORE the first short run (`backtest.py` SHORT_BAR): the exact mirror of the skeleton (EPS miss >= 5%, reaction <= -5% on >= 2x volume, short the open after D+1, stop = reaction-day HIGH, 2R target, 15d time-box) on 2021-01-01..2025-12-31, judged NET of an assumed 5%/yr borrow fee. `supports_short_shadow_book` only if avg net R > 0 AND total net R > 0 in >= 3 of the 5 entry years AND total net R > 0 without 2022 (the one down year for the index); avg net R > 0 but short of that = `positive_but_fragile`; avg net R <= 0 = `no_support`; any failed price download = `undeterminable` (rerun, failed cohorts are not cached). Computed in code (`backtest-short`); closed to revision. A pass is a case for the owner to consider a zero-capital short SHADOW book and nothing more -- never a live short, never a gate change (`allow_short` stays false). Not modelled: borrow availability, the per-name fee, the short-sale restriction after a 10% drop, squeeze gaps through the stop, delisted names. RESULT 2026-11-10 (the first and only run; 0 failed downloads): `no_support` -- n=818, hit rate 35.9%; gross avg -0.054R / total -43.9R; net of the 5%/yr fee avg -0.075R / total -61.6R. Net total R by entry year: 2021 +9.2, 2022 -10.9, 2023 +8.4, 2024 -49.5, 2025 -18.8 (positive in 2 of 5; -50.7R without 2022). Negative even at a 0% fee, so the fee assumption does not decide it (25%/yr: -0.162R). It LOST in 2022, the one year a short leg was supposed to earn its keep, while the long skeleton made +42.6R that year. Long skeleton, same range, same run: n=2302, +0.062R / +142.1R, matching the stored 2021-22 and 2023-25 baselines to the hundredth. Exits: 485 stops, 194 targets, 139 time-boxes. Survivorship runs AGAINST the short here: 214 of 7,660 miss candidates had no price history (2.8%, vs 0.5% on the long side); at the sample's pass rate that is about 24 missing trades, and the net total stays negative even if every one had paid the full 2R. CLOSED 11-10: no short shadow book. Do not re-propose the mirror skeleton, and do not re-parameterise it -- a threshold chosen after this result is a search over the same rows. Trades in state/backtest/short-2021-01-01-2025-12-31.json. |
| 14 | Hedged-book line | 2026-11-10 | Should live money carry an index hedge? Owner-initiated. REGISTERED 2026-11-10 before the first forward session: fixed ratio 0.5 of equity short VOO (read from the 07-24..11-09 record, beta about 0.5; NEVER re-fit); hedged day return = bot - 0.5 x VOO between consecutive benchmark marks, compounded. `shadow-status` -> hedged_book: `forward` (from the 11-09 mark) is the measurement; `in_sample` is reference only, NOT evidence (there: hedged +3.82% vs unhedged +5.08%, vol 12.2% vs 14.7%, max drawdown 5.51% vs 4.13%, worst day -2.74% vs -3.67% -- insurance, not alpha). No pass bar; read at the 100-trade verdict. Not modelled: dividends owed on the short, financing, rebalancing cost. No orders, no gate change. |

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
