"""External-signal ledger: does following someone else's disclosed buys pay?

Built 2026-09-08 to test copy-trading as a strategy CLASS rather than one
guru at a time. Sources are pluggable and measured identically, so insider
Form 4 buys, congressional disclosures, and a hand-logged X account all land
on the same scoreboard against the same benchmark.

Three design choices carry the whole method, because they are exactly what
casual copy-trade backtests get wrong:

1. ACTIONABLE PRICE, NOT SIGNAL PRICE. You cannot trade a tweet at the price
   it was posted at. Every signal records when it was PUBLISHED and,
   separately, the first session we could realistically have acted — by
   default the next trading session's open, via the NYSE calendar. Marking
   from the signal price is how copy-trade backtests manufacture returns
   that no follower could have captured.

2. FIXED-HORIZON FORWARD RETURNS, NOT COPIED EXITS. Disclosure is
   asymmetric: buys get announced, exits go quiet or never arrive. Inventing
   an exit for the source would measure our exit rule, not their signal — and
   copying only their entries would bias any book toward holding losers. So
   we score fixed horizons (5/15/60 sessions) and ask the narrow, honest
   question: did the ENTRY predict drift?

3. ALPHA, NOT RETURN. A semiconductor call during a semiconductor melt-up is
   not skill. Each signal names its own benchmark (SPY by default, SMH/XLK
   for sector-concentrated sources) and is scored net of it.

Measurement only. Nothing here places an order or influences the gate.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .calendar import is_trading_day

DIRECTIONS = ("buy", "sell")
HORIZONS = (5, 15, 60)          # trading sessions
DEFAULT_BENCHMARK = "SPY"


@dataclass
class Signal:
    id: str
    source: str                  # serenity | insider_form4 | congress | ...
    symbol: str
    direction: str               # buy | sell
    signal_date: str             # when the source published/disclosed it
    actionable_date: str         # first session we could have acted (>= signal)
    benchmark: str = DEFAULT_BENCHMARK
    note: str = ""
    logged_at: str = ""


def next_tradeable_session(d: date, max_lookahead: int = 10) -> date:
    """First trading session strictly AFTER d.

    Deliberately conservative: even a signal published mid-session is treated
    as actionable only at the next open. Overstating our own reaction speed is
    the single easiest way to fake an edge here.
    """
    for i in range(1, max_lookahead + 1):
        cand = d + timedelta(days=i)
        if is_trading_day(cand):
            return cand
    raise ValueError(f"no trading session within {max_lookahead} days of {d}")


def signal_id(source: str, symbol: str, signal_date: str) -> str:
    return f"{source.lower()}-{symbol.upper()}-{signal_date}"


def validate_signal(payload: dict) -> list[str]:
    errs = []
    if not str(payload.get("source", "")).strip():
        errs.append("source required (who the signal came from)")
    if not str(payload.get("symbol", "")).strip():
        errs.append("symbol required")
    if payload.get("direction") not in DIRECTIONS:
        errs.append(f"direction must be one of {DIRECTIONS}")
    try:
        date.fromisoformat(payload.get("signal_date", ""))
    except ValueError:
        errs.append("signal_date must be YYYY-MM-DD (when THEY disclosed it)")
    if not str(payload.get("note", "")).strip():
        errs.append("note required — what was actually claimed, in their words")
    return errs


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def append_jsonl(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


# -- scoring -------------------------------------------------------------------

def _fwd(closes, start_idx: int, n: int) -> float | None:
    """% change from start_idx to start_idx+n, or None if not enough bars."""
    if start_idx + n >= len(closes):
        return None
    a, b = float(closes[start_idx]), float(closes[start_idx + n])
    return round(100 * (b / a - 1), 2) if a else None


def score_signal(sig: dict, df, bench_df) -> dict | None:
    """Forward returns from the ACTIONABLE session, net of the benchmark.

    Returns None while no horizon has elapsed yet — a signal is never scored
    early, and never scored from a price we could not have traded.
    """
    dates = [ts.date() for ts in df.index]
    act = date.fromisoformat(sig["actionable_date"])
    idx = next((i for i, d in enumerate(dates) if d >= act), None)
    if idx is None:
        return None

    bdates = [ts.date() for ts in bench_df.index] if bench_df is not None else []
    bidx = next((i for i, d in enumerate(bdates) if d >= act), None)

    closes = list(df["Close"])
    bcloses = list(bench_df["Close"]) if bench_df is not None else []

    out = {"signal_id": sig["id"], "source": sig["source"],
           "symbol": sig["symbol"], "direction": sig["direction"],
           "benchmark": sig.get("benchmark", DEFAULT_BENCHMARK),
           "actionable_date": sig["actionable_date"],
           "actionable_close": round(float(closes[idx]), 4)}
    # Scoring is gated on the SIGNAL's horizon elapsing, not on alpha being
    # computable. Gating on alpha meant a benchmark data outage silently
    # discarded the whole record and left the signal "pending" forever — the
    # renders-as-benign family again. Forward returns are real data; keep
    # them and let alpha be None.
    any_scored = False
    for h in HORIZONS:
        r = _fwd(closes, idx, h)
        b = _fwd(bcloses, bidx, h) if bidx is not None else None
        out[f"fwd_{h}d_pct"] = r
        out[f"bench_{h}d_pct"] = b
        # Alpha only when BOTH legs exist — never impute a benchmark.
        alpha = round(r - b, 2) if (r is not None and b is not None) else None
        # A "sell" signal is a bet the name underperforms, so its alpha is
        # the negation: following it correctly means avoiding/shorting.
        if alpha is not None and sig["direction"] == "sell":
            alpha = -alpha
        out[f"alpha_{h}d_pct"] = alpha
        any_scored = any_scored or r is not None
    out["benchmark_available"] = bidx is not None
    return out if any_scored else None


def aggregate(scores: list[dict]) -> dict:
    """Per-source scoreboard. Hit rate is share of POSITIVE alpha, since
    beating the benchmark is the only claim worth testing."""
    by_source: dict[str, list] = {}
    for s in scores:
        by_source.setdefault(s["source"], []).append(s)

    out = {}
    for source, rows in sorted(by_source.items()):
        entry = {"n_signals": len(rows)}
        for h in HORIZONS:
            vals = [r[f"alpha_{h}d_pct"] for r in rows
                    if r.get(f"alpha_{h}d_pct") is not None]
            if vals:
                entry[f"h{h}"] = {
                    "n": len(vals),
                    "mean_alpha_pct": round(sum(vals) / len(vals), 2),
                    "median_alpha_pct": round(sorted(vals)[len(vals) // 2], 2),
                    "beat_benchmark": sum(1 for v in vals if v > 0),
                    "hit_rate": round(sum(1 for v in vals if v > 0) / len(vals), 3),
                }
            else:
                entry[f"h{h}"] = {"n": 0}
        out[source] = entry
    return out
