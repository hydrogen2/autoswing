"""Earnings forecast ledger — experiment #3 (measurement only, no capital).

The brain logs explicit pre-print predictions (beat/miss/inline + stock
reaction direction + confidence) in two tiers: "deep" (researched: peer
read-through, hiring signals, prior-call tone) and "quick" (cheap pass).
A scorer marks them against actual surprises and reaction-day moves.

The question this answers before any money moves: is the brain's hit rate
above coin-flip, is its confidence calibrated, and does the deep tier beat
the quick tier (i.e. does research depth pay)?

Ledger design is append-only and immutable: forecasts.jsonl (predictions,
never revised — one per symbol+report_date, first call stands) and
scores.jsonl (scoring events referencing forecast ids). Scoring rules are
deliberately conservative: a reaction call of up/down on a "flat" outcome
(|move| < 1%) scores as WRONG, not excluded.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

EPS_CALLS = ("beat", "miss", "inline")
REACTION_CALLS = ("up", "down")
TIERS = ("deep", "quick")

EPS_INLINE_BAND_PCT = 2.0     # |surprise| <= 2% counts as inline
REACTION_FLAT_BAND_PCT = 1.0  # |move| < 1% counts as flat
MARKET_CLOSE_ET = (16, 0)


@dataclass
class Forecast:
    id: str
    symbol: str
    made_at: str              # ISO timestamp (UTC)
    report_date: str          # YYYY-MM-DD
    timing: str               # bmo | amc | unknown
    tier: str                 # deep | quick
    eps_call: str             # beat | miss | inline
    # RETIRED for the quick tier on 2026-08-20: after n=46 the quick
    # reaction leg scored 41.3% with INVERTED calibration (the 50-60%
    # confidence bucket hit 36.7%), i.e. stated confidence was anti-signal.
    # RETIRED for the deep tier on 2026-08-27: at n=31 it scored 41.9%,
    # below the pre-agreed <45%-at-n>=30 bar, with the 80-100% confidence
    # bucket at 0/2 — the same inversion. Deep EPS calls stay (74.2%).
    # None = not forecast, which is excluded from the denominator rather
    # than scored wrong.
    reaction_call: str | None  # up | down | None
    confidence: float         # 0.5..1.0
    reasoning: str = ""
    # Consensus EPS the call was made against, captured at log time. The
    # scorer's yfinance fallback needs an estimate basis, and by scoring
    # time the calendar row this came from may have vanished (NB
    # 2026-09-11: the Nasdaq row moved on before the ±5d grace ran out).
    # None on rows logged before 2026-09-17 and when no consensus exists.
    eps_consensus: float | None = None


def validate_forecast(payload: dict) -> list[str]:
    errs = []
    if not payload.get("symbol", "").strip():
        errs.append("symbol required")
    if payload.get("eps_call") not in EPS_CALLS:
        errs.append(f"eps_call must be one of {EPS_CALLS}")
    rc = payload.get("reaction_call")
    if rc is not None and rc not in REACTION_CALLS:
        errs.append(f"reaction_call must be one of {REACTION_CALLS} or omitted (leg retired both tiers)")
    if payload.get("tier") not in TIERS:
        errs.append(f"tier must be one of {TIERS}")
    try:
        c = float(payload.get("confidence", -1))
        if not (0.5 <= c <= 1.0):
            errs.append("confidence must be in [0.5, 1.0]")
    except (TypeError, ValueError):
        errs.append("confidence must be a number")
    from datetime import date
    try:
        date.fromisoformat(payload.get("report_date", ""))
    except ValueError:
        errs.append("report_date must be YYYY-MM-DD")
    if payload.get("timing") not in ("bmo", "amc", "unknown"):
        errs.append("timing must be bmo|amc|unknown")
    if not payload.get("reasoning", "").strip():
        errs.append("reasoning required — a forecast without a why is a coin flip")
    ec = payload.get("eps_consensus")
    if ec is not None:
        try:
            float(ec)
        except (TypeError, ValueError):
            errs.append("eps_consensus must be a number when given")
    return errs


def forecast_id(symbol: str, report_date: str) -> str:
    return f"{symbol.upper()}-{report_date}"


def post_hoc_reason(report_date: str, timing: str, now_et) -> str | None:
    """Reject a "forecast" for a print that has already happened.

    Staleness family, inverted: the usual bug is treating a past report as
    upcoming. Here the same ambiguity would let a run transcribe a released
    number and score it as a prediction, silently inflating the hit rate
    the whole experiment exists to measure. Nothing would look wrong.

    Deterministic rules, conservative where the release time is unknowable:
      - report_date in the past: printed, whatever the timing.
      - same-day BMO: refused outright. Releases land anywhere from 06:00
        ET (BJ, 2026-08-21 at 06:45) and the premarket window runs at 08:00,
        so "before the open" does not mean "before this run". Forecast it
        the day before — which is already the practice.
      - same-day AMC: legitimate all session; post-hoc from the close.
      - same-day unknown timing: refused — cannot establish it hasn't printed.

    Returns None when the forecast is genuinely ex ante, else the reason.
    """
    from datetime import date

    rd = date.fromisoformat(report_date)
    today = now_et.date()
    if rd < today:
        return (f"report_date {report_date} is in the past — that print is out; "
                "a forecast logged after it is transcription, not prediction")
    if rd > today:
        return None
    if timing == "bmo":
        return ("same-day BMO: the release may already be out (they land from "
                "06:00 ET, before the premarket run) — log BMO forecasts the "
                "day before the report")
    if timing == "unknown":
        return ("same-day report with unknown timing: cannot establish it "
                "hasn't printed — log it the day before, or set bmo/amc")
    if (now_et.hour, now_et.minute) >= MARKET_CLOSE_ET:
        return ("same-day AMC logged at/after the 16:00 ET close — the print "
                "is either out or imminent")
    return None


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def append_jsonl(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


# -- scoring -------------------------------------------------------------------

def classify_eps(surprise_pct: float | None) -> str:
    if surprise_pct is None:
        return "unknown"
    if surprise_pct > EPS_INLINE_BAND_PCT:
        return "beat"
    if surprise_pct < -EPS_INLINE_BAND_PCT:
        return "miss"
    return "inline"


def classify_reaction(move_pct: float) -> str:
    if move_pct >= REACTION_FLAT_BAND_PCT:
        return "up"
    if move_pct <= -REACTION_FLAT_BAND_PCT:
        return "down"
    return "flat"


def awaiting_actuals(surprise_pct: float | None, move_pct: float | None,
                     grace_expired: bool) -> bool:
    """Defer scoring while ANY leg is missing and the grace window is open.

    Scoring with a missing leg burns that leg forever (one score event per
    forecast id, ledger append-only). VST/TTWO 2026-08-07 were scored with
    eps_actual "unknown" hours after a BMO report whose actuals the calendar
    hadn't published yet — a late data feed rendered as a permanent miss.
    """
    return (surprise_pct is None or move_pct is None) and not grace_expired


def needs_reaction_leg(tier: str) -> bool:
    """Deep tier still forecasts reactions; quick tier stopped 2026-08-20."""
    return tier == "deep"


def score_forecast(fc: dict, surprise_pct: float | None,
                   move_pct: float | None, scored_at: str) -> dict:
    eps_actual = classify_eps(surprise_pct)
    if not fc.get("reaction_call"):
        # Leg was never forecast (quick tier after 2026-08-20). Distinct from
        # "unknown", which means the actual is unavailable — both are excluded
        # from the hit-rate denominator, but only this one is deliberate.
        reaction_actual = "not_forecast"
    elif move_pct is not None:
        reaction_actual = classify_reaction(move_pct)
    else:
        reaction_actual = "unknown"
    return {
        "forecast_id": fc["id"],
        "tier": fc["tier"],
        "scored_at": scored_at,
        "surprise_pct": surprise_pct,
        "eps_actual": eps_actual,
        # None = unmeasured, never False: a False here reads as "graded wrong"
        # in the journal even though compute_stats excludes the leg (HUBG/PSEC
        # 2026-08-26 tripped a false staleness alarm exactly this way).
        "eps_correct": (None if eps_actual == "unknown"
                        else eps_actual == fc["eps_call"]),
        "move_pct": move_pct,
        "reaction_actual": reaction_actual,
        # Conservative: "flat" scores an up/down call as wrong.
        "reaction_correct": (None if reaction_actual not in REACTION_CALLS
                             + ("flat",)
                             else reaction_actual == fc.get("reaction_call")),
        "confidence": fc["confidence"],
        # True only if at least one leg was MEASURED. "not_forecast" is not a
        # measurement: a quick-tier row with eps_actual "unknown" has zero
        # measured legs and must not count as scored (IBN 2026-10-22 burned
        # this way and still inflated n_scored).
        "scorable": (eps_actual != "unknown"
                     or reaction_actual in REACTION_CALLS + ("flat",)),
    }


def compute_stats(forecasts: list[dict], scores: list[dict]) -> dict:
    by_id = {f["id"]: f for f in forecasts}
    scored_ids = {s["forecast_id"] for s in scores}
    out = {"pending": len([f for f in forecasts if f["id"] not in scored_ids]),
           "tiers": {}}
    for tier in TIERS:
        ss = [s for s in scores if s["tier"] == tier and s.get("scorable")]
        # Per-leg denominators: a leg whose actual is "unknown" is unmeasured,
        # not wrong — pooling it forces a miss into the hit rate (VST/TTWO
        # 2026-08-07 dragged quick-tier EPS down with unpublished actuals).
        eps_ss = [s for s in ss if s.get("eps_actual") != "unknown"]
        rx_ss = [s for s in ss
                 if s.get("reaction_actual") not in ("unknown", "not_forecast")]
        tier_stats = {
            "n_scored": len(ss),
            "eps_n": len(eps_ss),
            "eps_hit_rate": round(sum(s["eps_correct"] for s in eps_ss)
                                  / len(eps_ss), 3) if eps_ss else None,
            "reaction_n": len(rx_ss),
            "reaction_hit_rate": round(sum(s["reaction_correct"] for s in rx_ss)
                                       / len(rx_ss), 3) if rx_ss else None,
            "calibration": {},
        }
        for lo, hi, label in ((0.5, 0.6, "50-60"), (0.6, 0.7, "60-70"),
                              (0.7, 0.8, "70-80"), (0.8, 1.01, "80-100")):
            bucket = [s for s in rx_ss if lo <= s["confidence"] < hi]
            if bucket:
                tier_stats["calibration"][label] = {
                    "n": len(bucket),
                    "reaction_accuracy": round(
                        sum(s["reaction_correct"] for s in bucket) / len(bucket), 3),
                }
        out["tiers"][tier] = tier_stats
    out["depth_verdict"] = depth_verdict(scores)
    return out


# Pre-registered deep-vs-quick verdict (owner approved 2026-10-19 on the
# manager's 10-16 proposal). Deep went 73.1% -> 69.8% -> 66.7% -> 65.8% across
# four reads while quick sat ~70%; registering the rule BEFORE deep reaches
# n=100 stops either side arguing with whichever way the number lands.
# Discussion trigger only: the quick tier is unaffected and nothing changes
# automatically. CLOSED to revision.
DEPTH_VERDICT_N = 100
DEPTH_PREMIUM_PP = 3.0
DEPTH_VERDICT_REGISTERED = "2026-10-19"
DEPTH_VERDICT_RULE = ("first 100 deep EPS scores (scorable, actual known); quick "
                      "comparator = every quick EPS score dated at or before the "
                      "100th deep score; 'depth_premium' only if deep beats quick "
                      "by >= 3pp, else 'no_depth_premium'")


def _eps_hit_rate(rows: list[dict]) -> float | None:
    return round(100 * sum(r["eps_correct"] for r in rows) / len(rows), 1) if rows else None


def depth_verdict(scores: list[dict]) -> dict:
    """Frozen at the 100th deep EPS score: both samples are cut at that
    score's timestamp so later quick scores cannot move the comparator."""
    eps = sorted((s for s in scores
                  if s.get("scorable") and s.get("eps_actual") != "unknown"),
                 key=lambda s: s.get("scored_at", ""))
    deep_all = [s for s in eps if s["tier"] == "deep"]
    deep = deep_all[:DEPTH_VERDICT_N]
    frozen = len(deep) >= DEPTH_VERDICT_N
    cutoff = deep[-1].get("scored_at", "") if frozen else None
    quick = [s for s in eps if s["tier"] == "quick"
             and (cutoff is None or s.get("scored_at", "") <= cutoff)]
    out = {
        "rule": DEPTH_VERDICT_RULE, "registered": DEPTH_VERDICT_REGISTERED,
        "required_n": DEPTH_VERDICT_N, "premium_pp": DEPTH_PREMIUM_PP,
        "deep_n": len(deep), "deep_eps_hit_pct": _eps_hit_rate(deep),
        "quick_n": len(quick), "quick_eps_hit_pct": _eps_hit_rate(quick),
        "note": "discussion trigger only; quick tier unaffected",
    }
    if not frozen:
        out.update(verdict="pending", remaining=DEPTH_VERDICT_N - len(deep))
        return out
    if not quick:
        out.update(verdict="no_comparator", reasons=["no quick EPS scores"])
        return out
    out["frozen_at"] = cutoff
    margin = round(out["deep_eps_hit_pct"] - out["quick_eps_hit_pct"], 1)
    out["margin_pp"] = margin
    if margin >= DEPTH_PREMIUM_PP:
        out.update(verdict="depth_premium",
                   reasons=[f"deep {out['deep_eps_hit_pct']}% vs quick "
                            f"{out['quick_eps_hit_pct']}% = +{margin}pp >= {DEPTH_PREMIUM_PP}pp"])
    else:
        out.update(verdict="no_depth_premium",
                   reasons=[f"deep {out['deep_eps_hit_pct']}% vs quick "
                            f"{out['quick_eps_hit_pct']}% = {margin:+}pp < {DEPTH_PREMIUM_PP}pp"])
    return out
