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

## Parameter sensitivity sweep (run 2026-08-11, state/backtest/sweep-2023-2025.json)

18 variants, one factor at a time around the baseline, same 3-year window.
Read as robustness check; with 18 tries, the best t-stats (~2.4) are about
what the null predicts for the winner of 18 draws — do not treat any single
cell as significant.

| variant | n | avg R | t | total R | maxDD R | 2023 R |
|---------------|------|--------|------|-------|-----|-------|
| baseline | 1612 | +0.055 | 1.76 | 87.9 | 145 | −52.1 |
| min_move=3 | 2088 | +0.040 | 1.48 | 84.4 | 190 | −70.4 |
| min_move=4 | 1863 | +0.038 | 1.32 | 70.5 | 170 | −66.6 |
| min_move=6 | 1374 | +0.080 | 2.38 | 109.9 | 116 | −29.9 |
| min_move=8 | 977 | +0.073 | 1.82 | 70.8 | 76 | −9.2 |
| min_vol=1.5 | 1871 | +0.070 | 2.45 | 131.5 | 153 | −58.4 |
| min_vol=2.5 | 1290 | +0.050 | 1.44 | 63.8 | 110 | −34.3 |
| min_vol=3.0 | 927 | +0.065 | 1.59 | 60.5 | 69 | −21.6 |
| surprise=7.5 | 1444 | +0.070 | 2.13 | 100.9 | 121 | −42.2 |
| surprise=10 | 1290 | +0.054 | 1.56 | 69.4 | 122 | −56.3 |
| surprise=15 | 1029 | +0.027 | 0.69 | 27.6 | 98 | −50.5 |
| stop_cap=5 | 931 | +0.007 | 0.16 | 6.4 | 99 | −52.1 |
| stop_cap=6 | 1167 | +0.046 | 1.21 | 53.6 | 116 | −44.1 |
| stop_cap=10 | 1913 | +0.058 | 2.11 | 110.4 | 159 | −40.0 |
| pullback=1 | 1448 | +0.048 | 1.48 | 69.1 | 126 | −46.8 |
| pullback=5 | 1675 | +0.043 | 1.43 | 72.6 | 155 | −65.5 |
| hold=10 | 1612 | +0.033 | 1.13 | 53.1 | 115 | −33.4 |
| hold=20 | 1612 | +0.059 | 1.85 | 95.0 | 137 | −37.7 |

Takeaways:
1. **Sign-robust, size-thin, everywhere.** All 18 variants have positive
   avg R (+0.007 to +0.080). The baseline is not a fragile peak — it sits
   mid-plateau. There is no parameter setting that turns this skeleton
   into a strong strategy, and none that kills it either.
2. **The one strong, monotone dial is stop geometry.** Tightening the stop
   cap to 5% collapses the edge to +0.007R (t=0.16) — tight stops convert
   drift trades into coin flips. The playbook's AEIS rule (stop set by the
   instrument, never tightened) is empirically re-validated a third way.
3. **Weak reactions are the drag.** Lowering the move floor to 3–4% adds
   trades and subtracts money; raising it to 6–8% improves every metric
   (avg R, drawdown, 2023 damage). In-sample, so a hypothesis, not a
   ruling — the wide ledger records reaction_move per trade and can test
   it forward before any live change.
4. **Bigger surprises don't help** past ~7.5% (surprise=15 is the second-
   worst cell), and the volume dial is non-monotone (1.5x best, 2.5x
   worst, 3.0x fine) — i.e. noise; leave it alone.
5. **Nothing fixes 2023.** Every variant loses that year; the best
   (min_move=8, −9R) merely refuses to trade. The chop-regime problem is
   structural, not parametric — regime awareness is judgment-layer work.

No live parameter changes from this sweep: in-sample selection off 18
variants is exactly how strategies get overfit. The move-floor hypothesis
(#3) goes to the wide ledger for forward validation.
