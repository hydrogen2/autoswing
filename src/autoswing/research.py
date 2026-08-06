"""Research instruments: exit counterfactuals and the skip ledger.

Both interrogate OUR OWN decisions with data we already have — no new
capital, no new risk surface.

Exit counterfactuals: replay every live entry under alternative exit rules
(same daily-bar engine conservatism as the shadow book: stop-first on
ambiguous bars). Answers "are our exits leaving money on the table?"

Skip ledger: the brain logs every seriously-considered-but-rejected
candidate with a structured category; a scorer later measures what those
names did next. Answers "does the LLM's judgment layer beat the raw
scanner?" — the most important unmeasured claim in the project.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .manage import trading_days_between

SKIP_CATEGORIES = (
    "low_quality_beat",   # headline beat, rotten insides (one-offs, guidance)
    "sold_off",           # market rejected the print
    "stop_geometry",      # chart-correct stop too far / breaks at cap size
    "liquidity",          # too thin
    "capacity",           # book full / no slot
    "already_moved",      # chased too far / day-0 too hot
    "other",
)


# -- exit counterfactuals ------------------------------------------------------

@dataclass
class LiveTrade:
    symbol: str
    entry_date: str          # YYYY-MM-DD
    entry: float
    stop: float
    target: float
    quantity: int


def extract_live_trades(journal_dir: Path) -> list[LiveTrade]:
    """Approved, non-dry-run PEAD proposals from the journal — the bot's
    actual entries (entry price approximated by the limit)."""
    trades: dict[str, LiveTrade] = {}
    for f in sorted(journal_dir.glob("*.jsonl")):
        for line in f.read_text().splitlines():
            if '"gate.decision"' not in line:
                continue
            e = json.loads(line)
            if e.get("event") != "gate.decision" or e.get("dry_run"):
                continue
            if not e.get("decision", {}).get("approved"):
                continue
            p = e.get("proposal", {})
            if p.get("strategy", "pead-v1") != "pead-v1":
                continue
            if p.get("rationale") in ("healthcheck", "sizing sanity check"):
                continue
            key = f"{p['symbol'].upper()}-{e['ts'][:10]}"
            trades[key] = LiveTrade(
                symbol=p["symbol"].upper(), entry_date=e["ts"][:10],
                entry=float(p["entry_limit"]), stop=float(p["stop_loss"]),
                target=float(p["take_profit"]), quantity=int(p["quantity"]),
            )
    return list(trades.values())


def simulate_exit(trade: LiveTrade, df, rule: dict,
                  today: date | None = None) -> dict:
    """Replay one trade under an exit rule against daily bars.

    rule: {name, target_r (None = no target), timebox_days,
           trail_r (None = fixed stop; else trailing distance in R)}
    Stop-first on ambiguous bars. Open trades marked at last close.
    """
    entry_d = date.fromisoformat(trade.entry_date)
    risk = trade.entry - trade.stop
    stop = trade.stop
    target = (trade.entry + rule["target_r"] * risk
              if rule.get("target_r") else None)
    highest_close = trade.entry

    last_close = None
    for ts in df.index:
        d = ts.date()
        if d < entry_d:
            continue
        if today and d > today:
            break
        bar = df.loc[ts]
        last_close = float(bar["Close"])
        # Exit checks use the stop as it stood BEFORE this bar; the trail
        # updates from this bar's close only for the NEXT bar. Updating
        # first would test an end-of-day stop against the same day's low —
        # look-ahead (caught by test_trailing_stop_locks_in_gains).
        if float(bar["Low"]) <= stop:
            return _cf_result(trade, d, stop, "stop")
        if target and float(bar["High"]) >= target:
            return _cf_result(trade, d, target, "target")
        if trading_days_between(entry_d, d) >= rule["timebox_days"]:
            return _cf_result(trade, d, last_close, "timebox")
        if rule.get("trail_r"):
            highest_close = max(highest_close, last_close)
            stop = max(stop, highest_close - rule["trail_r"] * risk)
    return _cf_result(trade, today or entry_d, last_close or trade.entry,
                      "still_open")


def _cf_result(trade: LiveTrade, on: date, price: float, reason: str) -> dict:
    return {
        "symbol": trade.symbol, "entry_date": trade.entry_date,
        "exit_date": on.isoformat(), "exit_price": round(price, 4),
        "reason": reason,
        "pnl": round((price - trade.entry) * trade.quantity, 2),
        "r_multiple": round((price - trade.entry) /
                            (trade.entry - trade.stop), 2)
        if trade.entry != trade.stop else None,
    }


EXIT_RULES = [
    {"name": "baseline (2R target, 15d)", "target_r": 2.0, "timebox_days": 15},
    {"name": "wider target (3R, 15d)", "target_r": 3.0, "timebox_days": 15},
    {"name": "no target, 1R trailing stop", "target_r": None,
     "timebox_days": 15, "trail_r": 1.0},
    {"name": "tighter timebox (2R, 10d)", "target_r": 2.0, "timebox_days": 10},
]


def compare_exit_rules(trades: list[LiveTrade], history: dict,
                       today: date | None = None) -> dict:
    out = {}
    for rule in EXIT_RULES:
        results = []
        for t in trades:
            df = history.get(t.symbol)
            if df is None:
                continue
            results.append(simulate_exit(t, df, rule, today=today))
        closed = [r for r in results if r["reason"] != "still_open"]
        out[rule["name"]] = {
            "trades": len(results),
            "closed": len(closed),
            "total_pnl": round(sum(r["pnl"] for r in results), 2),
            "wins": len([r for r in closed if r["pnl"] > 0]),
            "avg_r": round(sum(r["r_multiple"] for r in closed) /
                           len(closed), 2) if closed else None,
            "results": results,
        }
    return out


# -- skip ledger ----------------------------------------------------------------

def validate_skip(payload: dict) -> list[str]:
    errs = []
    if not payload.get("symbol", "").strip():
        errs.append("symbol required")
    if payload.get("category") not in SKIP_CATEGORIES:
        errs.append(f"category must be one of {SKIP_CATEGORIES}")
    if not payload.get("reason", "").strip():
        errs.append("reason required")
    return errs


def score_skips(skips: list[dict], history: dict,
                today: date | None = None) -> dict:
    """Forward returns from the skip-day close: 5d, 15d, max runup/drawdown."""
    today = today or date.today()
    scored, pending = [], 0
    for s in skips:
        df = history.get(s["symbol"])
        skip_d = date.fromisoformat(s["date"])
        if df is None:
            continue
        after = [ts for ts in df.index if ts.date() >= skip_d]
        if not after or trading_days_between(skip_d, today) < 5:
            pending += 1
            continue
        base = float(df.loc[after[0]]["Close"])
        window = df.loc[after[0]:].head(16)
        closes = window["Close"].astype(float)
        entry = {
            "symbol": s["symbol"], "date": s["date"],
            "category": s["category"],
            "fwd_5d_pct": round(100 * (float(closes.iloc[min(5, len(closes) - 1)])
                                       / base - 1), 2),
            "fwd_15d_pct": round(100 * (float(closes.iloc[-1]) / base - 1), 2)
            if len(closes) >= 11 else None,
            "max_runup_pct": round(100 * (float(window["High"].max()) / base - 1), 2),
            "max_drawdown_pct": round(100 * (float(window["Low"].min()) / base - 1), 2),
        }
        scored.append(entry)

    by_cat: dict[str, list] = {}
    for e in scored:
        by_cat.setdefault(e["category"], []).append(e)
    summary = {}
    for cat, entries in by_cat.items():
        with15 = [e for e in entries if e["fwd_15d_pct"] is not None]
        summary[cat] = {
            "n": len(entries),
            "avg_fwd_5d_pct": round(sum(e["fwd_5d_pct"] for e in entries)
                                    / len(entries), 2),
            "avg_fwd_15d_pct": round(sum(e["fwd_15d_pct"] for e in with15)
                                     / len(with15), 2) if with15 else None,
        }
    return {"scored": scored, "pending": pending, "by_category": summary}
