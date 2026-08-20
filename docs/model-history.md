# Model history

The bot's judgment quality is part of what every live experiment measures
(lessons, wide-PEAD ledger, forecast calibration, skip ledger). A model
change is therefore a **regime boundary**: series that span one are not
apples-to-apples, and any analysis crossing a boundary must say so.

The model is pinned in `scripts/brain_run.sh` and `scripts/manager_run.sh`
(`MODEL="${AUTOSWING_MODEL:-...}"`), never inherited from the account
default — an account-level default change must not silently alter trading
behaviour mid-experiment. Override for a one-off run with
`AUTOSWING_MODEL=... scripts/brain_run.sh <window> --force`.

| From | To | Model | Notes |
|------|----|-------|-------|
| 2026-07-09 (inception) | 2026-08-19 | `claude-sonnet-5` | Inherited account default; not pinned until 08-20. All 15 closed live trades, the 13-lesson reflection backlog, forecast n≈34/15, and the wide/v2 ledgers to date were produced under Sonnet 5. |
| 2026-08-20 | — | `claude-opus-5` | Owner asked to try Opus 5 (2026-08-20). Pinned explicitly in both cron scripts at the same time. |

## Reading results across the 2026-08-20 boundary

- **Forecast calibration**: quick-tier n≈34 / deep n≈15 are Sonnet-era.
  Do not pool with post-08-20 scores when judging hit rates; report
  separately until the Opus-era sample is meaningful on its own.
- **Wide-PEAD ledger**: the mechanical skeleton is model-independent, but
  which candidates clear the brain's quality bar is not. Segment by entry
  date at the boundary.
- **Lessons**: the 13 backlog reflections are Sonnet-written. They remain
  valid inputs (they encode outcomes, not model opinions), but a change in
  lesson *style* after 08-20 is expected and is not a regression.
- **Cost/quota**: Opus draws materially more per run than Sonnet. Four
  brain windows plus one manager review per weekday. If quota pressure
  starts killing windows (see the 08-13 and 08-18 incidents), the cheapest
  lever is `AUTOSWING_MODEL=claude-sonnet-5` in the environment, or
  reverting the pin — no code change needed.
