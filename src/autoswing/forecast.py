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
    # Deep tier keeps it (n=20, still measuring). None = not forecast, which
    # is excluded from the denominator rather than scored wrong.
    reaction_call: str | None  # up | down | None
    confidence: float         # 0.5..1.0
    reasoning: str = ""


def validate_forecast(payload: dict) -> list[str]:
    errs = []
    if not payload.get("symbol", "").strip():
        errs.append("symbol required")
    if payload.get("eps_call") not in EPS_CALLS:
        errs.append(f"eps_call must be one of {EPS_CALLS}")
    rc = payload.get("reaction_call")
    if payload.get("tier") == "deep":
        if rc not in REACTION_CALLS:
            errs.append(f"reaction_call must be one of {REACTION_CALLS} for the deep tier")
    elif rc is not None and rc not in REACTION_CALLS:
        errs.append(f"reaction_call must be one of {REACTION_CALLS} or omitted (quick tier)")
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
        "eps_correct": (eps_actual != "unknown" and eps_actual == fc["eps_call"]),
        "move_pct": move_pct,
        "reaction_actual": reaction_actual,
        # Conservative: "flat" scores an up/down call as wrong.
        "reaction_correct": (reaction_actual in REACTION_CALLS
                             and reaction_actual == fc.get("reaction_call")),
        "confidence": fc["confidence"],
        "scorable": eps_actual != "unknown" or reaction_actual != "unknown",
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
    return out
