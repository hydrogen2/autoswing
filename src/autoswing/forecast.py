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


@dataclass
class Forecast:
    id: str
    symbol: str
    made_at: str              # ISO timestamp (UTC)
    report_date: str          # YYYY-MM-DD
    timing: str               # bmo | amc | unknown
    tier: str                 # deep | quick
    eps_call: str             # beat | miss | inline
    reaction_call: str        # up | down
    confidence: float         # 0.5..1.0
    reasoning: str = ""


def validate_forecast(payload: dict) -> list[str]:
    errs = []
    if not payload.get("symbol", "").strip():
        errs.append("symbol required")
    if payload.get("eps_call") not in EPS_CALLS:
        errs.append(f"eps_call must be one of {EPS_CALLS}")
    if payload.get("reaction_call") not in REACTION_CALLS:
        errs.append(f"reaction_call must be one of {REACTION_CALLS}")
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


def score_forecast(fc: dict, surprise_pct: float | None,
                   move_pct: float | None, scored_at: str) -> dict:
    eps_actual = classify_eps(surprise_pct)
    reaction_actual = classify_reaction(move_pct) if move_pct is not None else "unknown"
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
                             and reaction_actual == fc["reaction_call"]),
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
        n = len(ss)
        tier_stats = {
            "n_scored": n,
            "eps_hit_rate": round(sum(s["eps_correct"] for s in ss) / n, 3) if n else None,
            "reaction_hit_rate": round(sum(s["reaction_correct"] for s in ss) / n, 3) if n else None,
            "calibration": {},
        }
        for lo, hi, label in ((0.5, 0.6, "50-60"), (0.6, 0.7, "60-70"),
                              (0.7, 0.8, "70-80"), (0.8, 1.01, "80-100")):
            bucket = [s for s in ss if lo <= s["confidence"] < hi]
            if bucket:
                tier_stats["calibration"][label] = {
                    "n": len(bucket),
                    "reaction_accuracy": round(
                        sum(s["reaction_correct"] for s in bucket) / len(bucket), 3),
                }
        out["tiers"][tier] = tier_stats
    return out
