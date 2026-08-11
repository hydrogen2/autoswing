# Mechanical PEAD skeleton backtest — 2023–2025 results (run 2026-08-11)

Command: `autoswing backtest`, default params (surprise ≥5%, ≥3 estimates,
move ≥5% on ≥2x volume, ADV ≥$5M, stop = reaction low capped 8%, 2R target,
15-day timebox, $5k standardized notional). Per-trade detail in
`state/backtest/results-<year>-*.json`.

**Caveats apply to every number here** (see backtest-feasibility.md):
inferred reaction days, survivor-tilted (31 delisted names dropped — losers
are overrepresented among them, so true numbers are likely worse), no
judgment layer. In-sample; parameters were not fit to this data but were
also not chosen blind to the era.

## Headline

| year | n | hit rate | avg R | total R |
|------|-----|----------|--------|---------|
| 2023 | 541 | 38.4% | −0.096 | −52.1 |
| 2024 | 578 | 46.5% | +0.174 | +100.5 |
| 2025 | 493 | 42.2% | +0.080 | +39.5 |
| **all** | **1612** | **42.5%** | **+0.055** | **+87.9** |

Mean R **+0.055 per trade, t = 1.76** — positive but NOT statistically
significant at conventional thresholds, before the survivorship haircut.
**Max drawdown 145R** on the R-equity curve; 2023H1 alone was a −71R
stretch (avg −0.25R/trade for six months).

## What the decomposition says

- Exit mix: 51% stop (−1R), 27% timebox (avg **+0.47R**), 22% target (+2R).
  The timebox exits carrying positive expectancy means post-earnings drift
  does exist in the surviving cohort — the edge is real but thin, and half
  the funnel pays −1R for it.
- Regime dependence is the story: five of six half-years were positive
  (+0.06 to +0.19 avg R); 2023H1 destroyed the year. The skeleton has no
  defense against a chop regime — it just keeps paying stops.
- Factor splits (quartile avg R): bigger surprise does NOT help (+0.06 Q1
  vs +0.05 Q4); hotter reactions slightly HURT (volume-ratio Q4 −0.04,
  move Q4 −0.03). Chasing the most violent reactions is a mild negative —
  consistent with the playbook's no-chase and stop-geometry rules.

## Read

The mechanical skeleton alone is roughly breakeven-to-slightly-positive
with brutal regime drawdowns. It is NOT a green light to scale, and it is
NOT a condemnation of the live strategy: it defines the baseline the
judgment layer (news checks, quality-of-beat, regime awareness) must beat.
The wide shadow ledger now accrues exactly that comparison forward: wide
ledger ≈ this skeleton live; live book = skeleton + judgment + capacity.
If the judgment layer can't lift +0.055R meaningfully, PEAD v1 doesn't
clear the mission's 13–15% bar on its own.

Cheap next step when wanted: parameter sensitivity sweep over the cached
data (minutes per variant now) — e.g. move floor 3–8%, drift tolerance,
timebox length — reading it as robustness check, not optimization.
