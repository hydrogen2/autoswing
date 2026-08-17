"""Reflection memory: lessons from closed trades, fed back to the brain.

The brain is stateless by design — a fresh context every window. Until
2026-08-14 that meant it never read the outcome of its own past decisions:
when a trade stopped out or hit target, the next entry window had no memory
of it. This module closes the loop the way TradingAgents' decision log does
(store pending -> outcome known -> short reflection -> inject into future
prompts), kept deliberately terse so lessons never bloat the window.

Three pieces:
- pending_reflections(): closed trades (manage.position_closed events) with
  no lesson yet, enriched with the realized outcome (exit fill, R multiple,
  alpha vs the benchmark over the hold). Deterministic — no LLM here.
- The brain writes the reflection (2-4 sentences, JSON via `lesson-log`),
  anchored to that outcome. Immutable once written.
- lessons_context(): the last N lessons formatted for prompt injection,
  same-symbol first, then cross-symbol.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

REQUIRED = ("symbol", "closed_date", "thesis_held", "lesson")
THESIS_VALUES = ("held", "failed", "mixed", "unclear")


@dataclass
class ClosedTrade:
    symbol: str
    placed_date: str
    closed_date: str
    entry_limit: float
    stop_loss: float
    take_profit: float
    rationale: str
    strategy: str
    exit_price: float | None = None   # from fill evidence, else None
    exit_kind: str = "unknown"        # stop | target | timebox | manual | unknown

    @property
    def id(self) -> str:
        return f"{self.symbol}-{self.placed_date}"


def extract_closed_trades(journal_dir: Path) -> list[ClosedTrade]:
    """Every real close of a live PEAD position, from two journal paths:

    - manage.position_closed: position vanished from the broker (bracket
      leg filled, or a manual/spurious close) — carries the entry meta.
    - manage.review with enforced=true: deterministic time-box / pre-
      earnings exits close via broker.close_position and delete meta
      in-process, so they never emit position_closed. These are the
      strategy's PROFITABLE exits (drift that ran to the time-box); a
      lesson memory that missed them would learn only from stop-outs.
      Entry meta is rebuilt from the original approved gate.decision.

    Exit price: broker.recent_fills (side SLD, same symbol, same UTC day).
    Missing evidence leaves exit_price=None — the reflection then works
    from the thesis and the exit kind rather than a fabricated number.
    Entries with NO buy fill evidence (WDFC 07-10 cancelled unfilled,
    VOYG 08-06 ran away unfilled) are not trades and are excluded."""
    closes: dict[str, ClosedTrade] = {}
    sells: dict[str, list[float]] = {}   # "SYM-YYYY-MM-DD" -> fill prices
    buys: set[str] = set()               # "SYM-YYYY-MM-DD" with a BOT fill
    kinds: dict[str, str] = {}
    approved: dict[str, dict] = {}       # "SYM-YYYY-MM-DD" -> proposal
    KEYS = ('"manage.position_closed"', '"broker.recent_fills"',
            '"manage.review"', '"gate.decision"',
            '"broker.place_bracket_order"')
    for f in sorted(journal_dir.glob("*.jsonl")):
        for line in f.read_text().splitlines():
            if not any(k in line for k in KEYS):
                continue
            e = json.loads(line)
            ev = e.get("event")
            day = e["ts"][:10]
            if ev == "gate.decision":
                if e.get("dry_run") or not e.get("decision", {}).get("approved"):
                    continue
                p = e.get("proposal", {})
                if p.get("rationale") in ("healthcheck", "sizing sanity check"):
                    continue
                approved[f"{p['symbol'].upper()}-{day}"] = p
            elif ev == "broker.place_bracket_order":
                r = e.get("result", {})
                if any(o.get("role") == "entry" and o.get("status") == "Filled"
                       for o in r.get("orders", [])):
                    buys.add(f"{r['symbol'].upper()}-{day}")
            elif ev == "manage.position_closed":
                m = e.get("meta") or {}
                if not m.get("symbol"):
                    continue
                if m.get("strategy", "pead-v1") != "pead-v1":
                    continue
                if str(m.get("rationale", "")).startswith("adopted:"):
                    continue
                t = ClosedTrade(
                    symbol=m["symbol"].upper(), placed_date=m["placed_date"],
                    closed_date=day, entry_limit=float(m["entry_limit"]),
                    stop_loss=float(m["stop_loss"]),
                    take_profit=float(m["take_profit"]),
                    rationale=m.get("rationale", ""),
                    strategy=m.get("strategy", "pead-v1"),
                )
                closes[t.id] = t
            elif ev == "broker.recent_fills":
                for fill in e.get("result") or []:
                    key = f"{fill['symbol'].upper()}-{str(fill.get('time',''))[:10]}"
                    if fill.get("side") == "SLD" and fill.get("price"):
                        sells.setdefault(key, []).append(float(fill["price"]))
                    elif fill.get("side") == "BOT":
                        buys.add(key)
            elif ev == "manage.review":
                for p in (e.get("result") or {}).get("positions", []):
                    if not p.get("enforced"):
                        continue
                    sym = p["symbol"].upper()
                    action = p.get("action", "")
                    kind = ("timebox" if "timebox" in action
                            else "earnings" if "earnings" in action
                            else "manual")
                    kinds[f"{sym}-{day}"] = kind
                    # rebuild entry meta from the latest approval before this day
                    prior = sorted(k for k in approved
                                   if k.startswith(sym + "-") and k[len(sym)+1:] <= day)
                    if not prior:
                        continue
                    pk = prior[-1]
                    pr = approved[pk]
                    if pr.get("strategy", "pead-v1") != "pead-v1":
                        continue
                    t = ClosedTrade(
                        symbol=sym, placed_date=pk[len(sym)+1:], closed_date=day,
                        entry_limit=float(pr["entry_limit"]),
                        stop_loss=float(pr["stop_loss"]),
                        take_profit=float(pr["take_profit"]),
                        rationale=pr.get("rationale", ""),
                        strategy=pr.get("strategy", "pead-v1"),
                    )
                    closes.setdefault(t.id, t)

    out = []
    for t in closes.values():
        if not any(b.startswith(t.symbol + "-") and t.placed_date <= b[len(t.symbol)+1:] <= t.closed_date
                   for b in buys):
            continue  # never filled: not a trade
        key = f"{t.symbol}-{t.closed_date}"
        prices = sells.get(key)
        if prices:
            t.exit_price = round(sum(prices) / len(prices), 4)
        t.exit_kind = kinds.get(key) or _infer_kind(t)
        out.append(t)
    return sorted(out, key=lambda t: (t.closed_date, t.symbol))


def _infer_kind(t: ClosedTrade) -> str:
    """Bracket legs don't journal as manage.review enforcement; infer from
    where the fill landed relative to the bracket (tolerance 0.5%)."""
    if t.exit_price is None:
        return "unknown"
    if abs(t.exit_price - t.stop_loss) <= 0.005 * t.stop_loss or t.exit_price < t.stop_loss:
        return "stop"
    if abs(t.exit_price - t.take_profit) <= 0.005 * t.take_profit or t.exit_price > t.take_profit:
        return "target"
    return "unknown"


def outcome(t: ClosedTrade, bench_entry: float | None,
            bench_exit: float | None) -> dict:
    """Realized numbers for the reflection prompt. R is vs the planned
    stop distance (the risk the brain chose), alpha vs the benchmark over
    the same hold. Any leg unavailable -> None, never a guess."""
    risk = t.entry_limit - t.stop_loss
    o = {"exit_price": t.exit_price, "exit_kind": t.exit_kind,
         "return_pct": None, "r_multiple": None, "alpha_pct": None}
    if t.exit_price is not None:
        o["return_pct"] = round(100 * (t.exit_price / t.entry_limit - 1), 2)
        if risk > 0:
            o["r_multiple"] = round((t.exit_price - t.entry_limit) / risk, 2)
        if bench_entry and bench_exit:
            bench_ret = 100 * (bench_exit / bench_entry - 1)
            o["alpha_pct"] = round(o["return_pct"] - bench_ret, 2)
    return o


# -- lesson ledger -------------------------------------------------------------

def load_lessons(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def validate_lesson(payload: dict) -> list[str]:
    errs = []
    for k in REQUIRED:
        if not payload.get(k):
            errs.append(f"missing {k}")
    if payload.get("thesis_held") not in THESIS_VALUES:
        errs.append(f"thesis_held must be one of {THESIS_VALUES}")
    lesson = str(payload.get("lesson", ""))
    if len(lesson) > 600:
        errs.append("lesson must be <= 600 chars (terse: it is re-read every window)")
    if len(lesson.split()) < 8:
        errs.append("lesson too short to be useful (>= 8 words)")
    return errs


def lessons_context(lessons: list[dict], symbol: str | None = None,
                    n_same: int = 3, n_cross: int = 6) -> str:
    """Prompt-injectable summary: same-symbol lessons first (full), then
    the most recent cross-symbol lessons. Bounded so it never bloats."""
    if not lessons:
        return ""
    ordered = sorted(lessons, key=lambda l: l.get("closed_date", ""), reverse=True)
    same = [l for l in ordered if symbol and l["symbol"] == symbol.upper()][:n_same]
    cross = [l for l in ordered if not symbol or l["symbol"] != symbol.upper()][:n_cross]
    parts = []
    if same:
        parts.append(f"Past {symbol.upper()} trades (most recent first):")
        parts += [_fmt(l) for l in same]
    if cross:
        parts.append("Recent lessons from other closed trades:")
        parts += [_fmt(l) for l in cross]
    return "\n".join(parts)


def _fmt(l: dict) -> str:
    r = l.get("r_multiple")
    r_s = f"{r:+.1f}R" if isinstance(r, (int, float)) else "R n/a"
    a = l.get("alpha_pct")
    a_s = f", alpha {a:+.1f}%" if isinstance(a, (int, float)) else ""
    return (f"- {l['symbol']} {l.get('placed_date','?')}->{l['closed_date']} "
            f"[{l.get('exit_kind','?')}, {r_s}{a_s}, thesis {l['thesis_held']}]: "
            f"{l['lesson']}")
