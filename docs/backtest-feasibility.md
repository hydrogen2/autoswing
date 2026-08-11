# Backtest feasibility spike — 2026-08-11

Question: can we source good-enough historical earnings data to backtest the
mechanical PEAD rule set? Answer: **yes, with three stated caveats.** A full
backtest is feasible and worth building.

## What was probed

Nasdaq's earnings-calendar API (the same endpoint the live scan uses) was
queried at 6mo/1y/2y/3y/5y/7y/10y depths, plus a yfinance price-coverage
check on 60 sampled names from the 2021/2023 vintages and an internal-
consistency check of the surprise arithmetic. Probe scripts were run from
the session scratchpad (throwaway); raw results below.

## Findings

1. **History goes back at least 10 years.** Every probed date from
   2016-08-03 forward returned a populated calendar (219–477 rows/day).

2. **Field completeness ~75–80% at all depths.** Rows with the full
   actual + forecast + surprise triplet: 384/477 (2026), 294/364 (2024),
   236/294 (2021), 159/219 (2016). Estimate counts (`noOfEsts`) present on
   ~85–90% — enough to apply the thin-coverage filter historically.

3. **The record is frozen, not restated.** On 2023-08-09, 208/209 rows
   satisfy `surprise ≈ (actual − forecast)/|forecast|` exactly. The lone
   "outlier" (ECOR) was OUR parser dropping a bare-minus sign — fixed in
   `_money()` the same day. Consistency means the published forecast is the
   consensus the surprise was actually computed against: point-in-time
   enough for our purposes.

4. **Price coverage is strong.** 60/60 sampled names from the 2021 and
   2023 calendars have daily bars around their report dates in yfinance.

## The three caveats (state them in every result readout)

- **Reaction-day ambiguity.** BMO/AMC timing is `time-not-supplied` on all
  historical rows (only ~current-week rows carry it). The backtest must
  infer the reaction day: of report-day vs next-day, take the one with the
  abnormal move×volume. Self-consistent with a strategy that keys on the
  reaction, but it is an inference, not a fact.
- **Residual survivorship risk.** Perfect 60/60 price coverage is
  suspiciously clean. We cannot verify whether Nasdaq regenerates old
  calendar pages from its *current* symbol table (which would silently drop
  long-delisted names — exactly the blowups a long-only drift backtest
  most needs to see). Treat all backtest results as modestly
  survivor-tilted; never quote them as expected live performance.
- **The judgment layer is absent.** The brain's news check ("is this beat
  clean?") cannot be replayed historically. The backtest validates the
  mechanical skeleton only. July showed the judgment layer does real work.

## Recommended build (est. 1–2 sessions)

1. Fetch+cache layer: one calendar request per trading day, cached to disk
   (~250 requests/backtest-year, gentle pacing; cache makes reruns free).
2. Reuse `data/candidates.py` filter logic on the cached rows (surprise,
   volume ratio, drift window), with the reaction-day inference above.
3. Reuse `shadow.mark_position()` verbatim for simulation — the
   conservative stop-first fill model on daily bars already exists and is
   regression-tested. Standardized notional per trade, same as the wide
   shadow ledger, so backtest / wide-ledger / live series are comparable.
4. Output: hit rate, avg R, expectancy by year and by regime tag —
   framed strictly as "does the skeleton show any drift edge in-sample."

## Raw probe results

| date       | rows | full triplet | est. counts | timing known |
|------------|------|--------------|-------------|--------------|
| 2026-08-05 | 477  | 384          | 415         | 2            |
| 2026-02-04 | 161  | 136          | 147         | 0            |
| 2025-08-06 | 429  | 352          | 381         | 0            |
| 2024-08-07 | 364  | 294          | 317         | 0            |
| 2023-08-09 | 291  | 209          | 232         | 0            |
| 2021-08-04 | 294  | 236          | 257         | 0            |
| 2019-08-07 | 249  | 176          | 196         | 0            |
| 2016-08-03 | 219  | 159          | 182         | 0            |

Price coverage: 2023-08-09 sample 30/30, 2021-08-04 sample 30/30 (every-Nth
sampling across the cap spectrum, ≥5 daily closes required around the
report date).
