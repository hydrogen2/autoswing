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
from datetime import date, timedelta
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


# -- earnings-tone ledger -------------------------------------------------------
#
# Proposed 2026-09-18, green-lit 09-24. Every other instrument has now
# finished its job and pointed the same way: exits are best-of-five and
# settled, all 114 scored skips are negative at 15d (the judgment layer
# rejects genuine losers), and the 2023-25 backtest said the mechanical
# skeleton is ~breakeven so the judgment layer must earn the whole edge.
# That leaves ENTRY SELECTION as the only unexplained place — and it is
# currently recorded as free-text rationale that cannot be regressed.
#
# These three fields turn the distinction into data. The motivating pair:
# FPS (a genuine backlog/guidance rewrite) paid 2.9R while TCOM (an
# adjusted-only beat masking a GAAP loss) was rightly skipped — same "beat"
# label, opposite quality, and nothing in the system could tell them apart
# numerically.
#
# PRE-REGISTERED (set before any data exists, per the drift-exhaustion
# lesson): NO field influences a live decision until n>=100 candidates, and
# then only if its drift separation survives dropping the single best trade.
# Logged for EVERY seriously-evaluated candidate, entered or skipped —
# logging only the entries would measure our taste, not the fields.

GUIDANCE_DIRECTION = ("raised", "reaffirmed", "cut", "none", "unclear")
ONE_TIME_ITEMS = ("clean", "minor", "material", "unclear")
BACKLOG_REWRITE = ("yes", "no", "unclear")
TONE_MIN_N_FOR_VERDICT = 100


def validate_tone(payload: dict) -> list[str]:
    """All three fields required — an omitted field is not the same as
    'unclear', and letting it default would quietly bias the sample toward
    whatever the brain found easy to read."""
    errs = []
    if not str(payload.get("symbol", "")).strip():
        errs.append("symbol required")
    if payload.get("guidance_direction") not in GUIDANCE_DIRECTION:
        errs.append(f"guidance_direction must be one of {GUIDANCE_DIRECTION}")
    if payload.get("one_time_items") not in ONE_TIME_ITEMS:
        errs.append(f"one_time_items must be one of {ONE_TIME_ITEMS}")
    if payload.get("backlog_rewrite") not in BACKLOG_REWRITE:
        errs.append(f"backlog_rewrite must be one of {BACKLOG_REWRITE}")
    if payload.get("outcome") not in ("entered", "skipped"):
        errs.append("outcome must be 'entered' or 'skipped' — logging only "
                    "entries would measure our taste, not the fields")
    if not str(payload.get("evidence", "")).strip():
        errs.append("evidence required — what in the filing/call says this")
    return errs


def score_tone(rows: list[dict], history: dict,
               today: date | None = None) -> dict:
    """15-day forward drift grouped by each tone field's value.

    Drift is measured from the LOG-DAY close for every candidate, entered or
    skipped, so the comparison is like-for-like: this asks whether the field
    predicts what the stock did, not whether our entries worked.
    """
    today = today or date.today()
    scored, pending = [], 0
    for r in rows:
        df = history.get(r["symbol"])
        if df is None:
            continue
        d0 = date.fromisoformat(r["date"])
        after = [ts for ts in df.index if ts.date() >= d0]
        if not after or trading_days_between(d0, today) < 15:
            pending += 1
            continue
        window = df.loc[after[0]:].head(16)
        closes = window["Close"].astype(float)
        base = float(closes.iloc[0])
        if len(closes) < 11 or not base:
            pending += 1
            continue
        scored.append({**{k: r[k] for k in
                          ("symbol", "date", "outcome", "guidance_direction",
                           "one_time_items", "backlog_rewrite")},
                       "fwd_15d_pct": round(100 * (float(closes.iloc[-1]) / base - 1), 2)})

    by_field: dict[str, dict] = {}
    for field in ("guidance_direction", "one_time_items", "backlog_rewrite"):
        buckets: dict[str, list] = {}
        for s in scored:
            buckets.setdefault(s[field], []).append(s["fwd_15d_pct"])
        by_field[field] = {
            v: {"n": len(xs), "mean_fwd_15d_pct": round(sum(xs) / len(xs), 2)}
            for v, xs in sorted(buckets.items())
        }
    return {
        "scored": len(scored), "pending": pending, "by_field": by_field,
        "verdict_ready": len(scored) >= TONE_MIN_N_FOR_VERDICT,
        "min_n_for_verdict": TONE_MIN_N_FOR_VERDICT,
        "caveats": [
            "measurement only — no field may influence a live decision "
            f"until n>={TONE_MIN_N_FOR_VERDICT} and the separation survives "
            "dropping the single best trade (pre-registered 2026-09-24)",
            "fields are the brain's READING of a filing, so they carry its "
            "biases; a field that merely restates surprise_pct is not news",
            "drift is measured from the log-day close for entered AND "
            "skipped candidates alike, so it is not a P&L claim",
        ],
    }


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
                  today: date | None = None,
                  entry_at_open: bool = False) -> dict:
    """Replay one trade under an exit rule against daily bars.

    ENTRY DAY (fixed 2026-10-05): a live entry happens mid-session, so that
    day's high and low include prices from BEFORE the entry existed. Testing
    them against the bracket invented stop-outs that never happened: ASO
    (09-09) and FPS (09-15) both replayed as -1R entry-day stops while the
    real trades hit their targets (+$499, +$815). Those two false stops
    understated every exit-counterfactual baseline by about $1,900. The shadow
    books got this fix on 09-09 (be851b0); this simulator did not. Same
    convention now: on the entry day only the CLOSE is provably post-entry,
    so a level fills there only if the close is at or through it.
    entry_at_open=True (a replayed entry AT the open) keeps full-bar checks,
    because then the whole bar really is post-entry.

    rule: {name, target_r (None = no target), timebox_days,
           trail_r (None = fixed stop; else trailing distance in R),
           exhaust_day + exhaust_min_r (optional conditional exit: if the
           trade has not reached exhaust_min_r by exhaust_day, close it)}
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
        if d == entry_d and not entry_at_open:
            if last_close <= stop:
                return _cf_result(trade, d, stop, "stop")
            if target and last_close >= target:
                return _cf_result(trade, d, target, "target")
        else:
            if float(bar["Low"]) <= stop:
                return _cf_result(trade, d, stop, "stop")
            if target and float(bar["High"]) >= target:
                return _cf_result(trade, d, target, "target")
        held = trading_days_between(entry_d, d)
        if held >= rule["timebox_days"]:
            return _cf_result(trade, d, last_close, "timebox")
        # Conditional drift-exhaustion exit. Checked AFTER stop/target so a
        # trade that already resolved today is not re-attributed, and on the
        # CLOSE (not the intraday path) because the decision a live preclose
        # window could actually make is "where is it now", never "where did
        # it trade at some point today".
        if rule.get("exhaust_day") and held >= rule["exhaust_day"]:
            if (last_close - trade.entry) / risk <= rule["exhaust_min_r"]:
                return _cf_result(trade, d, last_close, "exhausted")
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
    # Proposed 2026-09-11, green-lit 09-15. Targets the documented
    # winner-fade pattern: a position going nowhere by day 8 rarely starts
    # drifting afterwards, so recycle the capital instead of waiting out
    # the full time-box.
    #
    # PRE-REGISTERED SUCCESS CRITERIA (set before the first run so this
    # cannot become a fishing expedition — the CRDO sweep lesson):
    # this earns a LIVE change only if it beats baseline on BOTH avg_r AND
    # total_pnl, AND the margin survives dropping the single best trade.
    # Anything less is reported as "no case", not as a near miss. It is the
    # FIFTH exit variant tested against ~30 trades; with enough variants one
    # will look good by chance.
    {"name": "drift-exhaustion (2R, 15d, exit <=+0.5R at day 8)",
     "target_r": 2.0, "timebox_days": 15,
     "exhaust_day": 8, "exhaust_min_r": 0.5},
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


# -- fill quality --------------------------------------------------------------

def extract_fills(journal_dir: Path) -> list[dict]:
    """Deduplicated executions from broker.recent_fills events. The hourly
    healthcheck journals the same day's fill list over and over; identity is
    (symbol, side, order_id, time) so each execution counts once."""
    seen: set[tuple] = set()
    fills = []
    for f in sorted(journal_dir.glob("*.jsonl")):
        for line in f.read_text().splitlines():
            if '"broker.recent_fills"' not in line:
                continue
            e = json.loads(line)
            for fill in e.get("result") or []:
                if not fill.get("price"):
                    continue
                key = (fill["symbol"].upper(), fill.get("side"),
                       fill.get("order_id"), str(fill.get("time", "")))
                if key in seen:
                    continue
                seen.add(key)
                fills.append({
                    "symbol": fill["symbol"].upper(), "side": fill.get("side"),
                    "shares": float(fill.get("shares", 0)),
                    "price": float(fill["price"]),
                    "day": str(fill.get("time", ""))[:10],
                })
    return fills


def fill_quality(journal_dir: Path, tolerance_pct: float = 0.5) -> dict:
    """Fill prices vs the prices the gate approved — the paper-to-live
    slippage baseline. Paper fills are SIMULATED; the point of recording
    their distribution now is to have the honest comparison series when
    live fills start arriving, not to celebrate zero slippage.

    Entries (BOT) match the same-day approved proposal, like
    extract_live_trades. Exits (SLD) match the latest prior approval for
    the symbol and are classified against its bracket: near/through the
    stop -> stop slippage, near/past the target -> target improvement,
    anything else (time-box, pre-earnings enforce, manual) is a market
    order with no promised price, so no slippage number is invented."""
    proposals: dict[str, dict] = {}
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
            if p.get("rationale") in ("healthcheck", "sizing sanity check"):
                continue
            proposals[f"{p['symbol'].upper()}-{e['ts'][:10]}"] = p

    tol = tolerance_pct / 100.0
    entries, exits, unmatched = [], [], []
    for fill in extract_fills(journal_dir):
        sym, day, price = fill["symbol"], fill["day"], fill["price"]
        if fill["side"] == "BOT":
            p = proposals.get(f"{sym}-{day}")
            if not p:
                unmatched.append(fill)
                continue
            limit = float(p["entry_limit"])
            entries.append(fill | {
                "entry_limit": limit,
                "improvement_bps": round(1e4 * (limit - price) / limit, 1),
            })
            continue
        if fill["side"] != "SLD":
            continue
        prior = sorted(k for k in proposals
                       if k.startswith(sym + "-") and k[len(sym) + 1:] <= day)
        if not prior:
            unmatched.append(fill)
            continue
        p = proposals[prior[-1]]
        stop, target = float(p["stop_loss"]), float(p["take_profit"])
        if price <= stop * (1 + tol):
            kind, ref = "stop", stop
        elif price >= target * (1 - tol):
            kind, ref = "target", target
        else:
            kind, ref = "market", None
        row = fill | {"exit_kind": kind, "reference": ref}
        if ref is not None:
            row["improvement_bps"] = round(1e4 * (price - ref) / ref, 1)
        exits.append(row)

    # improvement_bps is signed the same way on both sides: positive means
    # the fill beat the promised price (bought under limit / sold above the
    # stop or target), negative means slippage cost us money.
    def agg(rows):
        # share-weighted, because a 5-share tail fill should not count as
        # much as the 100-share body of the position
        bps = [(r["improvement_bps"], r["shares"]) for r in rows
               if "improvement_bps" in r]
        if not bps:
            return {"n": len(rows), "measured": 0}
        w = sum(s for _, s in bps) or 1
        return {
            "n": len(rows), "measured": len(bps),
            "mean_bps_weighted": round(sum(b * s for b, s in bps) / w, 1),
            "worst_bps": round(min(b for b, _ in bps), 1),
            "best_bps": round(max(b for b, _ in bps), 1),
            "at_reference": sum(1 for b, _ in bps if b == 0),
        }

    stops = [r for r in exits if r["exit_kind"] == "stop"]
    targets = [r for r in exits if r["exit_kind"] == "target"]
    market = [r for r in exits if r["exit_kind"] == "market"]
    return {
        "entries": agg(entries) | {"fills": entries},
        "stops": agg(stops) | {"fills": stops},
        "targets": agg(targets) | {"fills": targets},
        "market_exits": {"n": len(market), "fills": market,
                         "note": "no promised price (time-box/enforce/manual) "
                                 "— nothing to measure slippage against"},
        "unmatched": len(unmatched),
        "caveat": "paper fills are simulated by the broker; this series is "
                  "the baseline to compare LIVE fills against after "
                  "promotion, not evidence of real execution quality",
    }


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


# -- completed-session entry counterfactual --------------------------------------
# Proposed by the manager 2026-10-02, approved by the owner 2026-10-05 with two
# conditions (report with and without the trades that generated the idea; fix
# the pass bar before running). Everything below was committed BEFORE the
# replay first touched real trades, so none of it could be shaped by the
# result.
#
# The question: KMX (09-29) was bought on the reaction day itself, mid-session,
# and faded. The lesson the brain took -- wait for one completed session that
# holds the gains -- currently lives in the playbook as judgment. Should it be
# a hard rule?
#
# THE RULE, mechanically (daily bars only, applied uniformly to every trade):
#   - D   = reaction day (reconstructed the way the scanner does it).
#   - D+1 = confirmation session: it must CLOSE at or above D's close.
#   - Entry at the OPEN of D+2, same stop PRICE the live trade used (the stop
#     is set by the chart, not by the entry), target 2R from the new entry.
#   - No trade if D+1 fails to confirm, if the D+2 open is at or below the stop
#     (the setup already broke), or if the stop would sit more than 12% away
#     (the playbook's existing hard ceiling).
#
# SIZING: each replayed trade risks the SAME DOLLARS the live trade risked, so
# replay dollars = replay R x live dollar risk. Matching share count instead
# would resize every trade whose entry moved, and on a book that is net
# negative, quietly shrinking positions flatters whichever side shrank.
#
# PRE-REGISTERED PASS BAR (house standard, same shape as drift-exhaustion):
#   the verdict sample EXCLUDES the hypothesis-generating trades (KMX, SNPS) --
#   a rule learned from two trades is guaranteed to look good on those two.
#   On that sample the rule makes a case for becoming a hard rule only if
#     (1) total replay dollars  > total live dollars, AND
#     (2) total replay R        > total live R, AND
#     (3) the dollar gain is still > 0 after removing the single trade that
#         contributes most to it.
#   Anything less is "no_case": the lesson stays judgment. Fewer than 20
#   scorable trades in the sample is "insufficient_sample", not a near miss.
#   A pass is a case for the OWNER to consider, never an automatic change.
ENTRY_CF_REGISTERED = "2026-10-05"
ENTRY_CF_EXCLUDE = ("KMX", "SNPS")
ENTRY_CF_MAX_STOP_PCT = 12.0
ENTRY_CF_MIN_N = 20
ENTRY_CF_BASELINE = {"name": "baseline (2R target, 15d)",
                     "target_r": 2.0, "timebox_days": 15}
ENTRY_CF_RULE = ("confirm: session after the reaction day closes >= reaction-"
                 "day close; enter next open, live stop price, 2R target; no "
                 "trade if unconfirmed, open <= stop, or stop > 12% away")
ENTRY_CF_PASS_BAR = ("ex-KMX/SNPS: replay beats live on total dollars AND "
                     "total R, and the dollar gain survives dropping the most "
                     "helpful trade; n >= 20 scorable")


def find_report(symbol: str, entry_date: date, reports_for_day,
                lookback_days: int = 10):
    """The earnings report a trade was reacting to: the most recent calendar
    row for the symbol on or before the entry date. reports_for_day is
    injected (date -> list[Report]) so this stays testable offline."""
    for back in range(lookback_days + 1):
        day = entry_date - timedelta(days=back)
        for rep in reports_for_day(day):
            if rep.symbol.upper() == symbol.upper():
                return rep
    return None


def resolve_reaction_day(entry_date: date, stamps: list, heuristic: dict | None,
                         is_trading_day) -> dict:
    """Which session was the reaction day for a trade entered on entry_date.

    Added after the first replay (2026-10-05) showed the calendar-only route
    is a guess for EVERY historical trade: Nasdaq's old day-rows no longer
    carry bmo/amc, so reaction_metrics falls back to "whichever of D / D+1
    moved more". That picked the day AFTER the live entry for FPS and KMX --
    impossible, the brain cannot buy before the reaction -- and disagreed
    with the report's own timestamp on AVAV.

    Order of evidence:
      1. A timestamped report within 10 days before the entry: before 09:30
         ET or during the session reacts the same day, at/after 16:00 the
         next session. Deterministic.
      2. Otherwise the calendar heuristic, constrained by the one hard fact
         available: the reaction cannot be after the entry. If the heuristic's
         pick is, its runner-up day is used when that one is consistent.
    Anything still inconsistent is unresolved, never forced.
    """
    def next_session(d: date) -> date:
        d += timedelta(days=1)
        while not is_trading_day(d):
            d += timedelta(days=1)
        return d

    near = [ts for ts in stamps if 0 <= (entry_date - ts.date()).days <= 10]
    if near:
        ts = max(near)
        after_close = (ts.hour, ts.minute) >= (16, 0)
        rd = next_session(ts.date()) if after_close else ts.date()
        if not is_trading_day(rd):
            rd = next_session(rd)
        if rd <= entry_date:
            return {"reaction_date": rd.isoformat(), "source": "stamped",
                    "timing": "amc" if after_close else "bmo_or_intraday",
                    "ambiguous": False}
    if heuristic and heuristic.get("reaction_date"):
        if date.fromisoformat(heuristic["reaction_date"]) <= entry_date:
            return {"reaction_date": heuristic["reaction_date"],
                    "source": "heuristic", "ambiguous": True}
        alt = heuristic.get("alt_day_date")
        if alt and date.fromisoformat(alt) <= entry_date:
            return {"reaction_date": alt, "source": "heuristic_alt",
                    "ambiguous": True}
        return {"unresolved": "reaction day after live entry on every source"}
    return {"unresolved": "no timestamped report and no calendar row"}


def completed_session_entry(trade: LiveTrade, df, reaction_date: date) -> dict:
    """Apply the mechanical rule to one trade. Returns the would-be entry, a
    skip with its reason, or 'unscorable' when the bars cannot answer -- and
    unscorable is always reported, never folded into a skip."""
    dates = [ts.date() for ts in df.index]
    if reaction_date not in dates:
        return {"status": "unscorable", "why": "reaction day not in price data"}
    r = dates.index(reaction_date)
    if r + 1 >= len(dates):
        return {"status": "unscorable", "why": "confirmation session not traded yet"}
    reaction_close = float(df["Close"].iloc[r])
    confirm_close = float(df["Close"].iloc[r + 1])
    base = {"reaction_close": round(reaction_close, 4),
            "confirm_date": dates[r + 1].isoformat(),
            "confirm_close": round(confirm_close, 4)}
    if confirm_close < reaction_close:
        return {**base, "status": "skipped", "why": "no_confirmation"}
    if r + 2 >= len(dates):
        return {**base, "status": "unscorable", "why": "entry session not traded yet"}
    entry = float(df["Open"].iloc[r + 2])
    base.update(cf_entry_date=dates[r + 2].isoformat(), cf_entry=round(entry, 4))
    if entry <= trade.stop:
        return {**base, "status": "skipped", "why": "setup_broke"}
    stop_pct = 100 * (entry - trade.stop) / entry
    base["cf_stop_pct"] = round(stop_pct, 2)
    if stop_pct > ENTRY_CF_MAX_STOP_PCT:
        return {**base, "status": "skipped", "why": "stop_too_wide"}
    cf = LiveTrade(symbol=trade.symbol, entry_date=dates[r + 2].isoformat(),
                   entry=entry, stop=trade.stop,
                   target=entry + 2 * (entry - trade.stop), quantity=1)
    return {**base, "status": "entered", "why": "confirmed", "cf_trade": cf}


def _entry_cf_row(trade: LiveTrade, df, reaction: dict, today) -> dict:
    live = simulate_exit(trade, df, ENTRY_CF_BASELINE, today=today)
    risk_usd = round((trade.entry - trade.stop) * trade.quantity, 2)
    row = {
        "symbol": trade.symbol, "entry_date": trade.entry_date,
        "reaction_date": reaction["reaction_date"],
        "ambiguous_reaction_day": reaction.get("ambiguous", False),
        "reaction_source": reaction.get("source"),
        "live_risk_usd": risk_usd,
        "live_r": live["r_multiple"], "live_pnl": live["pnl"],
        "live_reason": live["reason"],
        # A live entry happens mid-session, but the daily bar's low may predate
        # it. Counted so the reader can see whether this convention matters.
        "live_entry_day_stop": (live["reason"] == "stop"
                                and live["exit_date"] == trade.entry_date),
    }
    rd = date.fromisoformat(reaction["reaction_date"])
    if rd > date.fromisoformat(trade.entry_date):
        row.update(cf_status="unscorable", cf_why="reaction day after live entry")
        return row
    row["live_entry_day"] = trading_days_between(
        rd, date.fromisoformat(trade.entry_date))
    cf = completed_session_entry(trade, df, rd)
    row.update({k: v for k, v in cf.items() if k not in ("cf_trade", "status", "why")})
    row.update(cf_status=cf["status"], cf_why=cf["why"])
    if cf["status"] == "entered":
        # The replayed entry is AT the open, so its whole first bar counts.
        res = simulate_exit(cf["cf_trade"], df, ENTRY_CF_BASELINE, today=today,
                            entry_at_open=True)
        row.update(cf_r=res["r_multiple"], cf_reason=res["reason"],
                   cf_exit_date=res["exit_date"],
                   cf_pnl=round(res["r_multiple"] * risk_usd, 2))
    elif cf["status"] == "skipped":
        row.update(cf_r=0.0, cf_pnl=0.0, cf_reason="no_trade")
    if row.get("cf_pnl") is not None:
        row["delta_pnl"] = round(row["cf_pnl"] - row["live_pnl"], 2)
    return row


def _entry_cf_totals(rows: list[dict]) -> dict:
    skips: dict[str, int] = {}
    for r in rows:
        if r["cf_status"] == "skipped":
            skips[r["cf_why"]] = skips.get(r["cf_why"], 0) + 1
    return {
        "n": len(rows),
        "live_total_pnl": round(sum(r["live_pnl"] for r in rows), 2),
        "cf_total_pnl": round(sum(r["cf_pnl"] for r in rows), 2),
        "live_total_r": round(sum(r["live_r"] or 0 for r in rows), 2),
        "cf_total_r": round(sum(r["cf_r"] or 0 for r in rows), 2),
        "live_wins": sum(1 for r in rows if r["live_pnl"] > 0),
        "cf_wins": sum(1 for r in rows if r["cf_pnl"] > 0),
        "cf_entered": sum(1 for r in rows if r["cf_status"] == "entered"),
        "cf_skipped": dict(sorted(skips.items())),
        "open_marks": sum(1 for r in rows if "still_open" in
                          (r["live_reason"], r.get("cf_reason"))),
    }


def entry_cf_verdict(rows: list[dict]) -> dict:
    """The pre-registered verdict over already-built rows."""
    scorable = [r for r in rows
                if r["cf_status"] not in ("unscorable", "duplicate_event")]
    sample = [r for r in scorable if r["symbol"] not in ENTRY_CF_EXCLUDE]
    out = {
        "rule": ENTRY_CF_RULE, "pass_bar": ENTRY_CF_PASS_BAR,
        "registered": ENTRY_CF_REGISTERED,
        "excluded_from_verdict": [r["symbol"] for r in scorable
                                  if r["symbol"] in ENTRY_CF_EXCLUDE],
        "unscorable": [{"symbol": r["symbol"], "entry_date": r["entry_date"],
                        "why": r["cf_why"]} for r in rows
                       if r["cf_status"] == "unscorable"],
        "duplicate_events": [{"symbol": r["symbol"], "entry_date": r["entry_date"],
                              "why": r["cf_why"]} for r in rows
                             if r["cf_status"] == "duplicate_event"],
        "verdict_sample": _entry_cf_totals(sample),
        "all_scorable": _entry_cf_totals(scorable),
    }
    s = out["verdict_sample"]
    if s["n"] < ENTRY_CF_MIN_N:
        out.update(verdict="insufficient_sample",
                   reasons=[f"{s['n']} scorable trades < {ENTRY_CF_MIN_N}"])
        return out
    gain = round(s["cf_total_pnl"] - s["live_total_pnl"], 2)
    best = max(sample, key=lambda r: r["delta_pnl"])
    gain_ex_best = round(gain - best["delta_pnl"], 2)
    out.update(dollar_gain=gain, dollar_gain_ex_best=gain_ex_best,
               most_helpful_trade={"symbol": best["symbol"],
                                   "entry_date": best["entry_date"],
                                   "delta_pnl": best["delta_pnl"]})
    reasons = []
    if gain <= 0:
        reasons.append(f"dollars: replay {s['cf_total_pnl']} vs live "
                       f"{s['live_total_pnl']} (no gain)")
    if s["cf_total_r"] <= s["live_total_r"]:
        reasons.append(f"R: replay {s['cf_total_r']} vs live {s['live_total_r']} "
                       "(no gain)")
    if gain > 0 and gain_ex_best <= 0:
        reasons.append(f"dollar gain {gain} rests on one trade "
                       f"({best['symbol']}): {gain_ex_best} without it")
    if reasons:
        out.update(verdict="no_case", reasons=reasons)
    else:
        out.update(verdict="case_for_hard_rule",
                   reasons=["beats live on dollars and R; gain survives "
                            "dropping the most helpful trade"])
    return out


def compare_entry_rule(trades: list[LiveTrade], history: dict,
                       reactions: dict, today: date | None = None) -> dict:
    """reactions: "SYMBOL-entry_date" -> {"reaction_date", "ambiguous"} or
    {"unresolved": why}. A trade whose reaction day could not be
    reconstructed is listed as unscorable, never silently dropped."""
    rows = []
    seen_events: dict[tuple, str] = {}
    for t in sorted(trades, key=lambda x: x.entry_date):
        key = f"{t.symbol}-{t.entry_date}"
        df = history.get(t.symbol)
        rx = reactions.get(key) or {"unresolved": "no reaction lookup"}
        # ONE EARNINGS EVENT IS ONE TRADE. A second live entry on the same
        # reaction (MMM 07-21 then 07-22, a re-entry after the stale-earnings
        # incident) maps to the SAME replayed trade; scoring both credited
        # the rule with one win twice. The first entry is the decision; the
        # later ones are listed and left out of every total.
        event = (t.symbol, rx.get("reaction_date"))
        if rx.get("reaction_date") and event in seen_events:
            rows.append({"symbol": t.symbol, "entry_date": t.entry_date,
                         "cf_status": "duplicate_event",
                         "cf_why": f"same reaction as {seen_events[event]}"})
            continue
        if rx.get("reaction_date"):
            seen_events[event] = key
        if df is None or "unresolved" in rx:
            rows.append({"symbol": t.symbol, "entry_date": t.entry_date,
                         "cf_status": "unscorable",
                         "cf_why": "no price data" if df is None
                         else rx["unresolved"]})
            continue
        rows.append(_entry_cf_row(t, df, rx, today))
    out = entry_cf_verdict(rows)
    out["rows"] = rows
    return out
