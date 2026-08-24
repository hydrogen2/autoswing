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
    actual entries (entry price approximated by the limit).

    A proposal only counts if the entry actually filled: same-day BUY fill
    evidence from broker.recent_fills, or an entry leg journaled as already
    "Filled" in broker.place_bracket_order. Approval is not a fill — VOYG
    2026-08-06 was approved and placed, ran away 9% unfilled, was cancelled,
    and still replayed here as a +2R win. Same-day matching (not order_id,
    which the broker reuses across resets) so a next-day re-entry fill can't
    validate an earlier unfilled attempt."""
    EVENT_KEYS = ('"gate.decision"', '"broker.place_bracket_order"',
                  '"broker.recent_fills"')
    trades: dict[str, LiveTrade] = {}
    filled: set[str] = set()  # "SYMBOL-YYYY-MM-DD" with buy-fill evidence
    for f in sorted(journal_dir.glob("*.jsonl")):
        for line in f.read_text().splitlines():
            if not any(k in line for k in EVENT_KEYS):
                continue
            e = json.loads(line)
            ev = e.get("event")
            if ev == "broker.recent_fills":
                for fill in e.get("result") or []:
                    if fill.get("side") == "BOT":
                        filled.add(f"{fill['symbol'].upper()}"
                                   f"-{str(fill.get('time', ''))[:10]}")
                continue
            if ev == "broker.place_bracket_order":
                r = e.get("result", {})
                if any(o.get("role") == "entry" and o.get("status") == "Filled"
                       for o in r.get("orders", [])):
                    filled.add(f"{r['symbol'].upper()}-{e['ts'][:10]}")
                continue
            if ev != "gate.decision" or e.get("dry_run"):
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
    return [t for key, t in trades.items() if key in filled]


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
    # Optional geometry: for stop_geometry skips the brain should log the
    # entry/stop it WOULD have used, so the counterfactual replays the real
    # declined trade instead of a reconstruction. Both or neither.
    e, st = payload.get("entry"), payload.get("stop")
    if (e is None) != (st is None):
        errs.append("entry and stop must be provided together (or both omitted)")
    elif e is not None:
        try:
            if float(e) <= float(st):
                errs.append("entry must be above stop (long-only)")
        except (TypeError, ValueError):
            errs.append("entry and stop must be numbers")
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


# -- stop-geometry skip counterfactual ----------------------------------------
#
# The playbook's ~8% stop ceiling makes us decline trades whose honest chart
# stop is wide. The skip ledger says that is our costliest skip category by
# raw forward return — but raw return is the wrong unit: a wide stop means
# fewer shares, so the same percentage move is a smaller R. This replays each
# declined trade under the SAME mechanics the live book uses (stop-first on
# ambiguous bars, 2R target, 15-day time-box) and reports R.
#
# Standing instrument, not a one-shot verdict: it reports verdict_ready only
# at n >= 20 (proposed by the manager 2026-08-21, approved 08-24).

REPLAY_MIN_N = 20
RECONSTRUCTION_MIN_PCT = 4.0  # below this, the reconstruction missed the reaction low


def replay_skip(skip: dict, df, max_hold_days: int = 15,
                today: date | None = None) -> dict | None:
    """One declined trade, replayed in R. Returns None if unreplayable.

    Geometry is 'logged' when the skip recorded the entry/stop the brain
    would have used, else 'reconstructed' from the bars: entry at the
    skip-day close, stop at the lowest low of the reaction window (the two
    sessions up to and including the skip day, mirroring 'below the
    reaction-day low'). Reconstruction is an approximation and is labelled
    as such — never pool the two bases without saying which is which.
    """
    from .shadow import ShadowPosition, mark_position

    today = today or date.today()
    skip_d = date.fromisoformat(skip["date"])
    idx = [i for i, ts in enumerate(df.index) if ts.date() >= skip_d]
    if not idx:
        return None
    i = idx[0]
    if skip.get("entry") is not None and skip.get("stop") is not None:
        entry, stop, basis = float(skip["entry"]), float(skip["stop"]), "logged"
    else:
        # Reconstruct: entry at the skip-day close, stop below the REACTION
        # day's low — the largest-move session in the 3 up to the skip day,
        # mirroring the playbook. A naive "lowest low of the last 2 bars"
        # picks a narrow-range day instead and invents implausibly tight
        # stops (ZBRA 0.66%, LIND 0.76% on 2026-08-06) for trades that were
        # declined precisely BECAUSE their honest stop was wide.
        entry = float(df["Close"].iloc[i])
        look = df.iloc[max(0, i - 2):i + 1]
        closes = look["Close"].astype(float)
        moves = [abs(closes.iloc[k] / closes.iloc[k - 1] - 1) if k else 0.0
                 for k in range(len(closes))]
        stop = float(look["Low"].iloc[moves.index(max(moves))])
        basis = "reconstructed"
    if entry <= stop:
        return None
    risk = entry - stop
    target = entry + 2 * risk
    pos = ShadowPosition(
        symbol=skip["symbol"], strategy="skip-replay",
        opened=df.index[i].date().isoformat(),
        entry_price=entry, quantity=1, stop_loss=stop, take_profit=target,
    )
    event = mark_position(pos, df, today, max_hold_days)
    distance_pct = round(100 * risk / entry, 2)
    if basis == "reconstructed" and distance_pct < RECONSTRUCTION_MIN_PCT:
        # A stop_geometry skip means the honest stop was wide (>~8%). A
        # reconstruction narrower than this did not find the reaction low,
        # so the R it would produce is noise. Report it, never average it.
        basis = "unreliable_reconstruction"
    out = {
        "symbol": skip["symbol"], "date": skip["date"], "basis": basis,
        "entry": round(entry, 4), "stop": round(stop, 4),
        "stop_distance_pct": distance_pct,
    }
    if event is None:
        last = float(df["Close"].iloc[-1])
        out |= {"status": "open", "r_multiple": round((last - entry) / risk, 3)}
    else:
        out |= {"status": "closed", "exit_reason": event["reason"],
                "r_multiple": round((event["exit_price"] - entry) / risk, 3),
                "days_held": event["days_held"]}
    return out


def replay_stop_geometry_skips(skips: list[dict], history: dict,
                               max_hold_days: int = 15,
                               today: date | None = None) -> dict:
    """Aggregate R for trades declined on stop geometry. Closed replays only
    feed the verdict; still-open ones are reported but not averaged."""
    results = []
    for s in skips:
        if s.get("category") != "stop_geometry":
            continue
        df = history.get(s["symbol"])
        if df is None:
            continue
        r = replay_skip(s, df, max_hold_days, today)
        if r:
            results.append(r)
    # The VERDICT counts logged geometry only. Reconstruction cannot recover
    # the stop the brain actually had in mind, and this category is defined
    # by that very number — so reconstructed rows are reported as indicative
    # and never mixed into the number that settles the policy question.
    logged_closed = [r for r in results
                     if r["status"] == "closed" and r["basis"] == "logged"]
    recon_closed = [r for r in results
                    if r["status"] == "closed" and r["basis"] == "reconstructed"]

    def agg(rows):
        rs = [r["r_multiple"] for r in rows]
        if not rs:
            return {"n": 0, "avg_r": None, "total_r": None, "win_rate": None}
        return {"n": len(rs), "avg_r": round(sum(rs) / len(rs), 3),
                "total_r": round(sum(rs), 2),
                "win_rate": round(sum(1 for r in rs if r > 0) / len(rs), 3)}

    return {
        "n_replayed": len(results),
        "still_open": sum(1 for r in results if r["status"] == "open"),
        "verdict": agg(logged_closed) | {
            "verdict_ready": len(logged_closed) >= REPLAY_MIN_N,
            "min_n_for_verdict": REPLAY_MIN_N,
            "basis": "logged geometry only",
        },
        "indicative_reconstructed": agg(recon_closed) | {
            "caveat": "entry/stop inferred from bars, not the brain's actual "
                      "geometry — directional only, never quote as the verdict",
        },
        "unreliable_reconstructions": sum(
            1 for r in results if r["basis"] == "unreliable_reconstruction"),
        "results": results,
    }
