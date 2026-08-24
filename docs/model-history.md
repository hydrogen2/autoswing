# Model history

The bot's judgment quality is part of what every live experiment measures
(lessons, wide-PEAD ledger, forecast calibration, skip ledger). A model
change is therefore a **regime boundary**: series that span one are not
apples-to-apples, and any analysis crossing a boundary must say so.

The model is pinned in `scripts/brain_run.sh` and `scripts/manager_run.sh`
(`MODEL_PRIMARY="${AUTOSWING_MODEL:-...}"`, `MODEL_FALLBACK="${AUTOSWING_MODEL_FALLBACK:-...}"`),
never inherited from the account default — an account-level default change must not silently alter trading
behaviour mid-experiment. Override for a one-off run with
`AUTOSWING_MODEL=... scripts/brain_run.sh <window> --force`.

| From | To | Model | Notes |
|------|----|-------|-------|
| 2026-07-09 (inception) | 2026-08-19 | `claude-sonnet-5` | Inherited account default; not pinned until 08-20. All 15 closed live trades, the 13-lesson reflection backlog, forecast n≈34/15, and the wide/v2 ledgers to date were produced under Sonnet 5. |
| 2026-08-20 | 2026-08-23 | `claude-opus-5` | Owner asked to try Opus 5. Pinned explicitly in both cron scripts at the same time. Three trading days only (08-20, 08-21, and part of 08-24) — too short to read anything from; its one visible contribution was finding the partial-volume bug on day one. |
| 2026-08-24 | — | `claude-fable-5` (fallback `claude-opus-5`) | Owner reserves Fable quota for autoswing and asked to run everything on it. Quota exhaustion now falls back to Opus automatically rather than losing the window. |

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
- **Cost/quota**: measured 2026-08-24 with matched cache states, Fable is
  ~2x Opus per unit of work (cold $0.210 vs $0.102; warm $0.027 vs $0.014),
  and Opus is well above Sonnet. Four brain windows plus one manager review
  per weekday. Cheapest lever under quota pressure is
  `AUTOSWING_MODEL=claude-sonnet-5` in the environment — no code change.

## Automatic quota fallback (2026-08-24)

Quota exhaustion is not transient, so retrying the same model just burns the
window — that is how 2026-08-18 lost both a midday window and the whole
day's review. Both scripts now detect quota/limit wording in the failure
output and retry ONCE on `MODEL_FALLBACK`, immediately (no backoff: the
primary will not recover inside this run, but the fallback has separate
headroom).

Deliberately narrow: the pattern matches session/usage/weekly/monthly limit
and explicit quota wording, and does NOT match 429 / "rate limit" /
"overloaded" — those are transient and the existing same-model retry is the
right response — nor auth failures, where a different model would not help.

Verified in a sandbox with a stub `claude` (2026-08-24): quota error falls
back and completes (`ok=1, model=claude-opus-5, fell_back=1`); a 529 does
NOT fall back and stays on the primary; the manager falls back, writes its
report, and the dead-man check correctly stays quiet. Every run logs which
model it used, so `grep "done (ok=" state/brain/logs/*` shows the mix.
