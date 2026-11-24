"""Regime-outcomes read: closed live trades split by tape at entry.

Research backlog #6, pre-registered 2026-11-20 (Friday proposal, 56 closed
trades at registration), owner-approved 2026-11-24 BEFORE the first read.
The buckets were fixed in the proposal and are not re-cut after the read:

- stressed: the entry date's benchmark mark has VIX close >= 25 OR
  spy_vs_20dma_pct <= -4.0;
- calm: otherwise;
- untagged: the entry date has no benchmark mark, or the mark lacks the
  regime fields (marks before 2026-08-06 carry none). Never guessed.

Output per bucket: n, total P&L, avg R, win rate. Descriptive and
discussion-only: no pass bar, no automatic change. It informs whether the
100-trade verdict gets a regime-stratified companion read and whether a
stressed-tape entry throttle is worth discussing (prompt/config, owner's
call).

A trade with no exit-price evidence or no recorded quantity still buckets
(the regime at entry is known) but is listed unscored and kept out of the
P&L / R / win-rate math — a stated gap, never a guessed number.
"""

from __future__ import annotations

import json
from pathlib import Path

# Registered bucket boundary. Changing either number is a revision of the
# registration and needs the owner's word, not a code change.
STRESSED_VIX = 25.0
STRESSED_SPY_VS_20DMA_PCT = -4.0

BUCKETS = ("stressed", "calm", "untagged")

# Proposals the risk gate sees that are not trades.
_PROBE_RATIONALES = ("healthcheck", "sizing sanity check")


def load_benchmark_marks(path: Path) -> dict[str, dict]:
    """state/benchmark.jsonl -> {date: mark row}. Later rows win a date."""
    marks: dict[str, dict] = {}
    if not path.exists():
        return marks
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        day = row.get("date")
        if day:
            marks[day] = row
    return marks


def extract_quantities(journal_dir: Path) -> dict[str, int]:
    """Share counts for real approved entries: "SYM-YYYY-MM-DD" -> quantity.

    Same filtering as lessons.extract_closed_trades: approved, not dry-run,
    not a probe proposal. The close event's meta carries no quantity, so
    dollar P&L has to come back to the approved proposal."""
    out: dict[str, int] = {}
    for f in sorted(journal_dir.glob("*.jsonl")):
        for line in f.read_text().splitlines():
            if '"gate.decision"' not in line:
                continue
            e = json.loads(line)
            if e.get("event") != "gate.decision":
                continue
            if e.get("dry_run") or not e.get("decision", {}).get("approved"):
                continue
            p = e.get("proposal", {})
            if p.get("rationale") in _PROBE_RATIONALES:
                continue
            qty = p.get("quantity")
            if p.get("symbol") and qty:
                out[f"{p['symbol'].upper()}-{e['ts'][:10]}"] = int(qty)
    return out


def classify_mark(mark: dict | None) -> str:
    """Bucket for one entry-date mark. Missing fields -> untagged, never
    guessed."""
    if not mark:
        return "untagged"
    vix = mark.get("vix_close")
    spy = mark.get("spy_vs_20dma_pct")
    if vix is None or spy is None:
        return "untagged"
    if float(vix) >= STRESSED_VIX or float(spy) <= STRESSED_SPY_VS_20DMA_PCT:
        return "stressed"
    return "calm"


def regime_outcomes(trades, marks: dict[str, dict],
                    quantities: dict[str, int]) -> dict:
    """The read. trades = lessons.extract_closed_trades() rows."""
    buckets = {b: {"rows": [], "unscored": []} for b in BUCKETS}
    for t in trades:
        mark = marks.get(t.placed_date)
        bucket = classify_mark(mark)
        entry = t.entry_fill if t.entry_fill is not None else t.entry_limit
        qty = quantities.get(f"{t.symbol}-{t.placed_date}")
        row = {
            "symbol": t.symbol, "opened": t.placed_date,
            "closed": t.closed_date, "exit_kind": t.exit_kind,
            "vix_close": None if not mark else mark.get("vix_close"),
            "spy_vs_20dma_pct": None if not mark else mark.get("spy_vs_20dma_pct"),
        }
        if t.exit_price is None or not qty:
            row["gap"] = ("no exit-price evidence" if t.exit_price is None
                          else "no recorded quantity")
            buckets[bucket]["unscored"].append(row)
            continue
        risk = entry - t.stop_loss
        row["pnl"] = round((t.exit_price - entry) * qty, 2)
        row["r_multiple"] = (round((t.exit_price - entry) / risk, 2)
                             if risk > 0 else None)
        buckets[bucket]["rows"].append(row)

    result_buckets = {}
    for b in BUCKETS:
        rows = buckets[b]["rows"]
        unscored = buckets[b]["unscored"]
        pnls = [r["pnl"] for r in rows]
        rs = [r["r_multiple"] for r in rows if r["r_multiple"] is not None]
        wins = sum(1 for p in pnls if p > 0)
        result_buckets[b] = {
            "n": len(rows) + len(unscored),
            "n_scored": len(rows),
            "total_pnl": round(sum(pnls), 2),
            "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
            "win_rate": round(wins / len(rows), 3) if rows else None,
            "rows": rows,
            "unscored": unscored,
        }

    return {
        "rule": ("closed live trades joined to the benchmark mark on the "
                 "entry date; stressed = VIX close >= 25 or spy_vs_20dma "
                 "<= -4% at entry, calm otherwise, untagged = regime "
                 "fields absent (never guessed); per bucket: n, total "
                 "P&L, avg R, win rate; descriptive only, no pass bar, "
                 "buckets not re-cut after the read"),
        "registered": "2026-11-20",
        "approved": "2026-11-24",
        "stressed_vix": STRESSED_VIX,
        "stressed_spy_vs_20dma_pct": STRESSED_SPY_VS_20DMA_PCT,
        "n_closed": len(trades),
        "buckets": result_buckets,
        "note": ("descriptive and discussion-only: no pass bar, no "
                 "automatic change; book breadth and any entry throttle "
                 "are config/prompt, the owner's alone"),
    }
