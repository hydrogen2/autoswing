"""Command-line tool surface. Every command prints a single JSON document.

This is what the scheduled agent calls; humans can run the same commands
to see exactly what the agent sees.
"""

import argparse
import json
import sys

from .broker import Broker, BracketProposal
from .config import load_config
from .journal import Journal


def main() -> None:
    parser = argparse.ArgumentParser(prog="autoswing")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("get-account", help="Account summary (net liq, cash, PnL)")
    sub.add_parser("get-positions", help="Open positions and working orders")

    q = sub.add_parser("get-quote", help="Delayed snapshot quote")
    q.add_argument("symbol")

    b = sub.add_parser(
        "place-bracket-order",
        help="Entry + stop-loss + take-profit as one atomic bracket",
    )
    b.add_argument("symbol")
    b.add_argument("action", choices=["BUY", "SELL"])
    b.add_argument("quantity", type=int)
    b.add_argument("--entry", type=float, required=True, help="entry limit price")
    b.add_argument("--stop", type=float, required=True, help="stop-loss price")
    b.add_argument("--target", type=float, required=True, help="take-profit price")

    c = sub.add_parser("cancel-order", help="Cancel a working order by id")
    c.add_argument("order_id", type=int)

    f = sub.add_parser(
        "flatten-all", help="EMERGENCY: cancel all orders, close all positions"
    )
    f.add_argument(
        "--i-am-sure", action="store_true",
        help="required acknowledgement that this closes everything",
    )

    sub.add_parser("smoke-test", help="Phase 0 exit test against the paper account")

    p = sub.add_parser(
        "propose-trade",
        help="Submit a trade proposal JSON through the risk gate; places the "
        "bracket only if every rule passes. This is the agent's ONLY entry path.",
    )
    p.add_argument(
        "proposal", help="path to proposal JSON file, or '-' to read stdin"
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="evaluate the gate but never place the order",
    )

    sub.add_parser("gate-status", help="Virtual equity, HWM, drawdown, kill switch")

    r = sub.add_parser(
        "gate-reset",
        help="HUMAN ONLY: clear a tripped kill switch and re-anchor equity",
    )
    r.add_argument("--i-am-sure", action="store_true")

    s = sub.add_parser(
        "scan-candidates",
        help="PEAD scan: recent reporters -> reaction metrics -> floors",
    )
    s.add_argument("--days-back", type=int, default=3)
    s.add_argument("--min-move", type=float, default=3.0,
                   help="min abs reaction move %% to qualify")

    n = sub.add_parser(
        "next-earnings", help="Next scheduled report date for a symbol (or 'unknown')"
    )
    n.add_argument("symbol")

    m = sub.add_parser(
        "manage-positions",
        help="Deterministic exits: time-box, pre-earnings, unverifiable earnings",
    )
    m.add_argument(
        "--enforce", action="store_true",
        help="actually close positions flagged for exit (default: report only)",
    )

    sub.add_parser(
        "benchmark-mark",
        help="Record today's virtual equity vs the benchmark (VOO) close",
    )

    sub.add_parser(
        "recent-fills", help="Today's executions (entries, stops, targets)"
    )

    jn = sub.add_parser(
        "journal-note", help="Append a free-form note (e.g. the brain's digest)"
    )
    jn.add_argument("note")

    sub.add_parser(
        "reconcile",
        help="Orphan-order guard: verify position/order consistency. Mode "
        "(shadow/enforce) comes from config and is human-only.",
    )

    mv = sub.add_parser(
        "scan-movers",
        help="v2 shadow: big movers NOT explained by recent earnings",
    )
    mv.add_argument("--min-move", type=float, default=5.0)

    sp = sub.add_parser(
        "shadow-propose",
        help="v2 shadow: run a proposal through the real gate, open a "
        "VIRTUAL position if approved. Never places orders.",
    )
    sp.add_argument("proposal", help="proposal JSON path, or '-' for stdin")

    sub.add_parser(
        "shadow-mark",
        help="v2 shadow: mark virtual positions vs real prices, close on "
        "stop/target/timebox (preclose task)",
    )

    sub.add_parser("shadow-status", help="v2 shadow: open book + ledger stats")

    su = sub.add_parser(
        "scan-upcoming",
        help="forecast exp: who reports in the next N days (consensus, timing)",
    )
    su.add_argument("--days", type=int, default=2)

    fl = sub.add_parser(
        "forecast-log",
        help="forecast exp: record an immutable pre-print prediction",
    )
    fl.add_argument("forecast", help="forecast JSON path, or '-' for stdin")

    sub.add_parser(
        "forecast-score",
        help="forecast exp: score pending predictions against actuals (preclose)",
    )

    sub.add_parser("forecast-stats", help="forecast exp: hit rates + calibration by tier")

    sub.add_parser(
        "exit-counterfactuals",
        help="research: replay all live entries under alternative exit rules",
    )

    ls = sub.add_parser(
        "log-skip",
        help="research: record a considered-but-skipped candidate (structured)",
    )
    ls.add_argument("skip", help="skip JSON path, or '-' for stdin")

    sub.add_parser(
        "skip-outcomes",
        help="research: what skipped candidates did next, by skip category",
    )

    args = parser.parse_args()
    config = load_config()
    journal = Journal(config.journal_dir)
    _arm_watchdog(journal, args.command)

    try:
        if args.command == "journal-note":
            result = journal.record("brain.note", note=_resolve_note(args.note))
        elif args.command in ("scan-candidates", "next-earnings", "scan-movers",
                              "shadow-mark", "shadow-status", "scan-upcoming",
                              "forecast-log", "forecast-score", "forecast-stats",
                              "exit-counterfactuals", "log-skip", "skip-outcomes"):
            result = _dispatch_data(config, journal, args)
        else:
            with Broker(config, journal) as broker:
                result = _dispatch(broker, args)
    except Exception as e:
        msg = _error_text(e)
        journal.record("cli.error", command=args.command, error=msg,
                       error_type=type(e).__name__)
        print(json.dumps({"ok": False, "error": msg}))
        sys.exit(1)

    print(json.dumps({"ok": True, "result": result}, indent=2, default=str))


def _arm_watchdog(journal: Journal, command: str) -> None:
    """Hard wall-clock limit on every CLI invocation. ib_async waits on some
    broker responses without a timeout; a half-alive gateway once hung
    gate-status for 15h holding the run lock and blackout the whole day
    (2026-07-16). No command has a legitimate reason to exceed this."""
    import os
    import signal

    limit = int(os.environ.get("AUTOSWING_CMD_TIMEOUT", "180"))

    def _die(signum, frame):
        try:
            journal.record("cli.watchdog_timeout", command=command, limit_s=limit)
        finally:
            print(json.dumps({"ok": False,
                              "error": f"watchdog: {command} exceeded {limit}s"}))
            os._exit(2)

    signal.signal(signal.SIGALRM, _die)
    signal.alarm(limit)


def _resolve_note(note: str, stdin=None) -> str:
    # Honor the `-`=stdin convention that propose-trade already uses. The
    # brain pipes digests as `... | journal-note -`; recording the literal
    # "-" silently drops the digest. On 2026-07-28 three digests were lost
    # this way (recovered only because the brain noticed and re-posted).
    if note == "-":
        stream = sys.stdin if stdin is None else stdin
        return stream.read().strip()
    return note


def _error_text(e: BaseException) -> str:
    # str() of e.g. TimeoutError() or ConnectionError() is "", which left
    # cli.error journal entries with no diagnostic at all; fall back to repr.
    return str(e) or repr(e)


def _dispatch(broker: Broker, args):
    if args.command == "get-account":
        return broker.get_account()
    if args.command == "get-positions":
        return broker.get_positions()
    if args.command == "get-quote":
        return broker.get_quote(args.symbol)
    if args.command == "place-bracket-order":
        return broker.place_bracket_order(
            BracketProposal(
                symbol=args.symbol,
                action=args.action,
                quantity=args.quantity,
                entry_limit=args.entry,
                stop_loss=args.stop,
                take_profit=args.target,
            )
        )
    if args.command == "cancel-order":
        return broker.cancel_order(args.order_id)
    if args.command == "flatten-all":
        if not args.i_am_sure:
            raise ValueError("flatten-all requires --i-am-sure")
        return broker.flatten_all()
    if args.command == "smoke-test":
        return _smoke_test(broker)
    if args.command == "propose-trade":
        return _propose_trade(broker, args)
    if args.command == "gate-status":
        gate = _make_gate(broker)
        return gate.status(broker.account_state())
    if args.command == "gate-reset":
        if not args.i_am_sure:
            raise ValueError("gate-reset requires --i-am-sure")
        gate = _make_gate(broker)
        before = gate.status(broker.account_state())
        gate.reset_kill()
        broker.journal.record("gate.reset", before=before)
        return {"reset": True, "state_before": before}
    if args.command == "manage-positions":
        return _manage_positions(broker, enforce=args.enforce)
    if args.command == "recent-fills":
        return broker.recent_fills()
    if args.command == "reconcile":
        return _reconcile(broker)
    if args.command == "shadow-propose":
        return _shadow_propose(broker, args)
    if args.command == "benchmark-mark":
        return _benchmark_mark(broker)
    raise ValueError(f"unknown command {args.command!r}")


def _meta_path():
    from .config import PROJECT_ROOT
    return PROJECT_ROOT / "state" / "positions.json"


def _manage_positions(broker: Broker, enforce: bool, meta_path=None):
    from datetime import date

    from .data.earnings import next_earnings_date
    from .manage import PositionMeta, evaluate_position, load_meta, save_meta

    meta_path = meta_path or _meta_path()
    meta = load_meta(meta_path)
    snapshot = broker.get_positions()
    held = {p["symbol"]: p for p in snapshot["positions"] if p["quantity"] != 0}
    working = {o["symbol"] for o in snapshot["open_orders"]}

    # Long-only strategy: a negative broker quantity is never ours by
    # intent (e.g. an orphaned stop selling into a book the gateway had
    # already blanked). Never manage it as a healthy long, never adopt
    # it, never auto-trade against it — flag it for a human.
    shorts = {s: p for s, p in held.items() if p["quantity"] < 0}
    for sym, pos in shorts.items():
        broker.journal.record(
            "manage.position_mismatch", symbol=sym,
            quantity=pos["quantity"], avg_cost=pos.get("avg_cost"),
            detail="broker reports a SHORT position in a long-only strategy; "
                   "refusing to manage or adopt it — human must flatten",
        )

    # Reconcile: drop meta for closed positions; adopt untracked ones today
    # (conservative: their time-box starts now, and they still get the
    # earnings check like everything else).
    suspect = set()
    for sym in list(meta):
        if sym not in held:
            if sym in working:
                # Position missing while its exit orders are still live is
                # impossible for a real close (a bracket fill cancels the
                # other leg). The gateway's overnight restart returns blank
                # position feeds; don't let one erase our stops/time-box.
                suspect.add(sym)
                broker.journal.record(
                    "manage.snapshot_suspect", symbol=sym,
                    detail="position missing but exit orders still working; "
                           "keeping metadata, skipping this pass",
                )
                continue
            broker.journal.record("manage.position_closed", symbol=sym,
                                  meta=meta[sym].__dict__)
            del meta[sym]
    adopted = []
    for sym in held:
        if sym not in meta and sym not in shorts:
            meta[sym] = PositionMeta(
                symbol=sym, placed_date=date.today().isoformat(),
                entry_limit=held[sym]["avg_cost"], stop_loss=0.0, take_profit=0.0,
                rationale="adopted: position existed without metadata",
            )
            adopted.append(sym)

    report = []
    for sym, pos in shorts.items():
        report.append({
            "symbol": sym, "action": "unexpected_short",
            "detail": f"URGENT: broker shows {pos['quantity']:g} shares but "
                      "the strategy is long-only; not managed, not adopted — "
                      "human must flatten",
            "next_earnings": None, "enforced": False,
        })
    # Iterate a snapshot: enforce deletes from meta below, and mutating the
    # dict mid-iteration raises "dictionary changed size during iteration"
    # — which on 2026-07-21 aborted the enforce loop after MMM had already
    # been closed, risking later positions left un-enforced or orphaned.
    for sym, m in list(meta.items()):
        if sym in shorts:
            continue
        if sym in suspect:
            report.append({
                "symbol": sym, "action": "hold",
                "detail": "snapshot suspect: position missing but exit "
                          "orders working; management skipped this pass",
                "next_earnings": None, "enforced": False,
            })
            continue
        ned = next_earnings_date(sym)
        action, detail = evaluate_position(
            sym, m.placed_date, ned, broker.config.strategy
        )
        entry = {"symbol": sym, "action": action, "detail": detail,
                 "next_earnings": ned, "enforced": False}
        if enforce and action != "hold":
            entry["close_result"] = broker.close_position(sym)
            entry["enforced"] = True
            del meta[sym]
        report.append(entry)

    save_meta(meta_path, meta)
    result = {"positions": report, "adopted_untracked": adopted, "enforce": enforce}
    broker.journal.record("manage.review", result=result)
    return result


def _shadow_propose(broker: Broker, args):
    """Full gate evaluation, virtual position on approval. NEVER places."""
    from datetime import date

    from .shadow import ShadowPosition, load_book, save_book

    raw = sys.stdin.read() if args.proposal == "-" else open(args.proposal).read()
    payload = json.loads(raw)
    payload.setdefault("strategy", "news-v2")
    proposal = _build_proposal(payload)

    gate = _make_gate(broker)
    decision = gate.evaluate(proposal, broker.account_state())

    entry_price = None
    opened = False
    if decision.approved:
        quote = broker.get_quote(proposal.symbol)
        entry_price = quote.get("last") or quote.get("close") or proposal.entry_limit
        pos_path, _ = _shadow_paths()
        book = load_book(pos_path)
        if proposal.symbol.upper() not in book:
            book[proposal.symbol.upper()] = ShadowPosition(
                symbol=proposal.symbol.upper(),
                strategy=proposal.strategy,
                opened=date.today().isoformat(),
                entry_price=float(entry_price),
                quantity=proposal.quantity,
                stop_loss=proposal.stop_loss,
                take_profit=proposal.take_profit,
                rationale=proposal.rationale,
            )
            save_book(pos_path, book)
            opened = True

    result = {
        "shadow": True,
        "approved": decision.approved,
        "opened_virtual": opened,
        "entry_price": entry_price,
        "decision": decision.to_dict(),
    }
    broker.journal.record("shadow.proposal", proposal=payload, result={
        "approved": decision.approved, "opened_virtual": opened,
        "entry_price": entry_price,
        "failed_rules": [r.rule for r in decision.rules if not r.passed],
    })
    return result


def _reconcile(broker: Broker):
    from datetime import datetime, timezone

    from .config import PROJECT_ROOT
    from .manage import load_meta
    from .reconcile import (
        Observation, OrderObs, evaluate, load_state, save_state,
    )

    cfg = broker.config.reconcile
    mode = cfg.get("mode", "shadow")
    state_path = PROJECT_ROOT / "state" / "reconcile_state.json"

    snapshot = broker.get_positions()
    account = broker.get_account()
    cash = float(account["summary"]["TotalCashValue"]["value"])
    obs = Observation(
        ts=datetime.now(timezone.utc).isoformat(),
        positions={p["symbol"]: p["quantity"] for p in snapshot["positions"]},
        orders=[
            OrderObs(
                order_id=o["order_id"], symbol=o["symbol"], action=o["action"],
                order_type=o["type"], quantity=o["quantity"], status=o["status"],
            )
            for o in snapshot["open_orders"]
        ],
        fills=broker.recent_fills(),
        cash=cash,
    )
    intent = {
        sym: {"stop_loss": m.stop_loss, "quantity": None}
        for sym, m in load_meta(_meta_path()).items()
    }

    new_state, decisions, notes = evaluate(obs, load_state(state_path), intent, cfg)
    save_state(state_path, new_state)

    actions = []
    for d in decisions:
        if mode != "enforce":
            actions.append({**d, "executed": False, "mode": mode,
                            "note": "SHADOW: would have acted"})
            continue
        if d["action"] == "cancel_orphans":
            results = [broker.cancel_order(oid) for oid in d["order_ids"]]
            actions.append({**d, "executed": True, "results": results})
        elif d["action"] == "replace_stop":
            r = broker.place_protective_stop(
                d["symbol"], int(d["quantity"]), d["stop_price"]
            )
            actions.append({**d, "executed": True, "results": [r]})

    result = {
        "mode": mode,
        "consistent": not decisions and not notes,
        "notes": notes,
        "decisions": actions,
        "state": {s: v.status for s, v in new_state.items()},
    }
    broker.journal.record("reconcile.report", result=result)
    return result


def _merge_benchmark_entry(existing, entry):
    """Merge a new daily benchmark mark into the series, deduping by date.

    The series is one row per calendar day. If benchmark-mark runs more than
    once in a day, the later mark must REPLACE the earlier row rather than
    append a duplicate — on 2026-08-03 an errant premarket benchmark-mark
    wrote a stale early row that the authoritative preclose mark then
    duplicated, leaving two 08-03 rows that double-count in any per-day
    aggregation. Last-write-wins per date, chronological order preserved.
    Also self-heals a series that already contains same-date duplicates.
    """
    by_date = {}
    for row in existing:
        by_date[row["date"]] = row
    by_date[entry["date"]] = entry
    return list(by_date.values())


def _benchmark_mark(broker: Broker):
    import json as _json
    import os
    from datetime import date

    # Preclose-only: an intraday mark is a stale data point (2026-08-03
    # duplicate-row incident). Unset AUTOSWING_WINDOW (manual/cron-less
    # invocation) is allowed; a declared non-preclose window is refused.
    window = os.environ.get("AUTOSWING_WINDOW", "")
    if window and window != "preclose":
        raise ValueError(
            f"benchmark-mark is preclose-only; refused in window {window!r}"
        )

    from .config import PROJECT_ROOT
    from .data.prices import fetch_history

    gate = _make_gate(broker)
    status = gate.status(broker.account_state())

    bench_sym = broker.config.strategy.get("benchmark_symbol", "VOO")
    hist = fetch_history([bench_sym, "SPY", "^VIX"], period="3mo")
    bench_close = float(hist[bench_sym]["Close"].iloc[-1]) if bench_sym in hist else None

    # Regime tags: joined to trades post-hoc for conditional-performance
    # research ("does PEAD pay in storms?"). Passive collection only.
    regime = {}
    if "SPY" in hist and len(hist["SPY"]) >= 20:
        spy = hist["SPY"]["Close"].astype(float)
        regime["spy_vs_20dma_pct"] = round(
            100 * (float(spy.iloc[-1]) / float(spy.tail(20).mean()) - 1), 2)
    if "^VIX" in hist and len(hist["^VIX"]):
        regime["vix_close"] = round(float(hist["^VIX"]["Close"].iloc[-1]), 2)

    path = PROJECT_ROOT / "state" / "benchmark.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if path.exists():
        for line in path.read_text().strip().splitlines():
            if line:
                existing.append(_json.loads(line))
    first = existing[0] if existing else None

    entry = {
        "date": date.today().isoformat(),
        "virtual_equity": status["virtual_equity"],
        "benchmark_close": bench_close,
        "drawdown_pct": status["drawdown_pct"],
        "kill_tripped": status["kill_tripped"],
        **regime,
    }
    if first and first.get("benchmark_close") and bench_close:
        entry["bot_return_pct"] = round(
            100 * (status["virtual_equity"] / first["virtual_equity"] - 1), 2
        )
        entry["benchmark_return_pct"] = round(
            100 * (bench_close / first["benchmark_close"] - 1), 2
        )
    series = _merge_benchmark_entry(existing, entry)
    with open(path, "w") as f:
        for row in series:
            f.write(_json.dumps(row) + "\n")
    broker.journal.record("benchmark.mark", result=entry)
    return entry


def _dispatch_data(config, journal: Journal, args):
    if args.command == "scan-candidates":
        from .data.candidates import scan

        result = scan(config.risk, days_back=args.days_back, min_move_pct=args.min_move)
        journal.record(
            "data.scan_candidates",
            scanned=result["scanned"], passing=result["passing"],
            price_data_missing=result["price_data_missing"],
            price_data_missing_symbols=result["price_data_missing_symbols"],
            symbols=[c["symbol"] for c in result["candidates"]],
        )
        return result
    if args.command == "next-earnings":
        from .data.earnings import next_earnings_date

        return {"symbol": args.symbol.upper(),
                "next_earnings_date": next_earnings_date(args.symbol)}
    if args.command == "scan-movers":
        from .data.movers import scan_movers

        result = scan_movers(config.risk, min_move_pct=args.min_move)
        journal.record("shadow.scan_movers", scanned=result["scanned"],
                       passing=result["passing"],
                       symbols=[c["symbol"] for c in result["candidates"]])
        return result
    if args.command == "shadow-mark":
        return _shadow_mark(config, journal)
    if args.command == "shadow-status":
        from .config import PROJECT_ROOT
        from .shadow import ledger_stats, load_book

        book = load_book(PROJECT_ROOT / "state" / "shadow" / "positions.json")
        return {
            "open": [p.__dict__ for p in book.values()],
            "ledger": ledger_stats(PROJECT_ROOT / "state" / "shadow" / "ledger.jsonl"),
        }
    if args.command == "scan-upcoming":
        return _scan_upcoming(args.days, journal)
    if args.command == "forecast-log":
        return _forecast_log(args, journal)
    if args.command == "forecast-score":
        return _forecast_score(journal)
    if args.command == "forecast-stats":
        from .config import PROJECT_ROOT
        from .forecast import compute_stats, load_jsonl

        d = PROJECT_ROOT / "state" / "forecast"
        return compute_stats(load_jsonl(d / "forecasts.jsonl"),
                             load_jsonl(d / "scores.jsonl"))
    if args.command == "exit-counterfactuals":
        from .config import PROJECT_ROOT
        from .data.prices import fetch_history
        from .research import compare_exit_rules, extract_live_trades

        trades = extract_live_trades(PROJECT_ROOT / "journal")
        history = fetch_history(sorted({t.symbol for t in trades}), period="6mo")
        comparison = compare_exit_rules(trades, history)
        journal.record("research.exit_counterfactuals", summary={
            name: {k: v for k, v in stats.items() if k != "results"}
            for name, stats in comparison.items()
        })
        return comparison
    if args.command == "log-skip":
        return _log_skip(args, journal)
    if args.command == "skip-outcomes":
        from .config import PROJECT_ROOT
        from .data.prices import fetch_history
        from .forecast import load_jsonl
        from .research import score_skips

        skips = load_jsonl(PROJECT_ROOT / "state" / "research" / "skips.jsonl")
        if not skips:
            return {"scored": [], "pending": 0, "by_category": {}}
        history = fetch_history(sorted({s["symbol"] for s in skips}), period="3mo")
        result = score_skips(skips, history)
        journal.record("research.skip_outcomes",
                       by_category=result["by_category"],
                       pending=result["pending"])
        return result
    raise ValueError(f"unknown data command {args.command!r}")


def _log_skip(args, journal: Journal):
    from datetime import date

    from .config import PROJECT_ROOT
    from .forecast import append_jsonl
    from .research import validate_skip

    raw = sys.stdin.read() if args.skip == "-" else open(args.skip).read()
    payload = json.loads(raw)
    errs = validate_skip(payload)
    if errs:
        raise ValueError("invalid skip: " + "; ".join(errs))
    entry = {
        "symbol": payload["symbol"].upper(),
        "date": payload.get("date", date.today().isoformat()),
        "category": payload["category"],
        "reason": payload["reason"],
    }
    append_jsonl(PROJECT_ROOT / "state" / "research" / "skips.jsonl", entry)
    journal.record("research.skip_logged", **entry)
    return {"logged": f"{entry['symbol']}-{entry['date']}",
            "category": entry["category"]}


def _forecast_paths():
    from .config import PROJECT_ROOT
    d = PROJECT_ROOT / "state" / "forecast"
    return d / "forecasts.jsonl", d / "scores.jsonl"


def _scan_upcoming(days: int, journal: Journal):
    from datetime import date, timedelta

    from .data.earnings import fetch_calendar_day
    from .data.prices import fetch_history

    today = date.today()
    rows = []
    for offset in range(days + 1):
        d = today + timedelta(days=offset)
        if d.weekday() >= 5:
            continue
        for r in fetch_calendar_day(d):
            rows.append({
                "symbol": r.symbol, "company": r.company,
                "report_date": r.report_date, "timing": r.timing,
                "eps_forecast": r.eps_forecast, "num_estimates": r.num_estimates,
                "market_cap": r.market_cap,
            })
    # Enrich the biggest 30 by market cap with liquidity data.
    rows.sort(key=lambda r: r["market_cap"] or 0, reverse=True)
    top = [r["symbol"] for r in rows[:30]]
    history = fetch_history(top, period="1mo")
    for r in rows[:30]:
        df = history.get(r["symbol"])
        if df is not None and len(df) >= 5:
            r["last_close"] = round(float(df["Close"].iloc[-1]), 2)
            r["adv_dollar_20d"] = round(float((df["Close"] * df["Volume"]).mean()), 0)
    journal.record("forecast.scan_upcoming", count=len(rows),
                   enriched=[r["symbol"] for r in rows[:30]])
    return {"count": len(rows), "reporters": rows[:60]}


def _forecast_log(args, journal: Journal):
    from datetime import datetime, timezone

    from .forecast import (
        Forecast, append_jsonl, forecast_id, load_jsonl, validate_forecast,
    )

    raw = sys.stdin.read() if args.forecast == "-" else open(args.forecast).read()
    payload = json.loads(raw)
    errs = validate_forecast(payload)
    if errs:
        raise ValueError("invalid forecast: " + "; ".join(errs))

    fpath, _ = _forecast_paths()
    fid = forecast_id(payload["symbol"], payload["report_date"])
    if any(f["id"] == fid for f in load_jsonl(fpath)):
        raise ValueError(
            f"forecast {fid} already exists — predictions are immutable, "
            "the first call stands"
        )
    fc = Forecast(
        id=fid, symbol=payload["symbol"].upper(),
        made_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        report_date=payload["report_date"], timing=payload["timing"],
        tier=payload["tier"], eps_call=payload["eps_call"],
        reaction_call=payload["reaction_call"],
        confidence=float(payload["confidence"]),
        reasoning=payload["reasoning"],
    )
    from dataclasses import asdict
    append_jsonl(fpath, asdict(fc))
    journal.record("forecast.logged", **asdict(fc))
    return {"logged": fid, "tier": fc.tier}


def _forecast_score(journal: Journal):
    from datetime import date, datetime, timezone

    from .data.earnings import fetch_calendar_day
    from .data.prices import fetch_history, reaction_metrics
    from .forecast import append_jsonl, load_jsonl, score_forecast

    fpath, spath = _forecast_paths()
    forecasts = load_jsonl(fpath)
    scored_ids = {s["forecast_id"] for s in load_jsonl(spath)}
    today = date.today()
    due = [f for f in forecasts
           if f["id"] not in scored_ids
           and date.fromisoformat(f["report_date"]) <= today]
    if not due:
        return {"scored": 0, "pending_future": len(forecasts) - len(scored_ids)}

    calendar_cache: dict[str, dict] = {}
    history = fetch_history(sorted({f["symbol"] for f in due}), period="1mo")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    results, still_pending = [], 0
    for f in due:
        rdate = date.fromisoformat(f["report_date"])
        key = f["report_date"]
        if key not in calendar_cache:
            calendar_cache[key] = {
                r.symbol: r for r in fetch_calendar_day(rdate)
            }
        report = calendar_cache[key].get(f["symbol"])
        surprise = report.surprise_pct if report else None

        df = history.get(f["symbol"])
        reaction = (reaction_metrics(f["symbol"], df, rdate, f["timing"])
                    if df is not None else None)
        move = reaction.move_pct if reaction else None

        grace_expired = (today - rdate).days > 5
        if surprise is None and move is None and not grace_expired:
            still_pending += 1  # actuals not out yet; retry next run
            continue
        entry = score_forecast(f, surprise, move, now)
        append_jsonl(spath, entry)
        results.append({k: entry[k] for k in
                        ("forecast_id", "eps_correct", "reaction_correct",
                         "eps_actual", "reaction_actual", "scorable")})
    journal.record("forecast.scored", scored=len(results),
                   awaiting_actuals=still_pending, results=results)
    return {"scored": len(results), "awaiting_actuals": still_pending,
            "results": results}


def _shadow_paths():
    from .config import PROJECT_ROOT
    d = PROJECT_ROOT / "state" / "shadow"
    return d / "positions.json", d / "ledger.jsonl"


def _shadow_mark(config, journal: Journal):
    from datetime import date

    from .data.prices import fetch_history
    from .shadow import load_book, mark_position, save_book

    pos_path, ledger_path = _shadow_paths()
    book = load_book(pos_path)
    if not book:
        return {"open": 0, "closed_today": []}

    history = fetch_history(sorted(book), period="3mo")
    max_hold = int(config.strategy.get("max_hold_days", 15))
    closed = []
    for sym in list(book):
        df = history.get(sym)
        if df is None:
            continue  # no data today; try again tomorrow
        event = mark_position(book[sym], df, date.today(), max_hold)
        if event:
            ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with open(ledger_path, "a") as f:
                import json as _json
                f.write(_json.dumps(event) + "\n")
            journal.record("shadow.close", **{k: v for k, v in event.items()
                                              if k != "event"})
            closed.append(event)
            del book[sym]
    save_book(pos_path, book)
    return {"open": len(book), "closed_today": closed}


def _make_gate(broker: Broker):
    from .config import PROJECT_ROOT
    from .risk_gate import RiskGate

    return RiskGate(
        risk_config=broker.config.risk,
        state_path=PROJECT_ROOT / "state" / "gate_state.json",
    )


def _build_proposal(payload: dict):
    """Build a TradeProposal, naming bad fields instead of raising a bare
    TypeError. On 2026-08-06 the brain omitted "action" and got
    "TradeProposal.__init__() missing 1 required positional argument" —
    a Python internals message it had to guess its way past mid-window."""
    from dataclasses import MISSING, fields

    from .risk_gate import TradeProposal

    spec = fields(TradeProposal)
    known = {f.name for f in spec}
    required = {f.name for f in spec
                if f.default is MISSING and f.default_factory is MISSING}
    problems = []
    missing = sorted(required - payload.keys())
    if missing:
        problems.append("missing required field(s): " + ", ".join(missing))
    unknown = sorted(payload.keys() - known)
    if unknown:
        problems.append("unknown field(s): " + ", ".join(unknown))
    if problems:
        raise ValueError("invalid proposal: " + "; ".join(problems))
    return TradeProposal(**payload)


def _propose_trade(broker: Broker, args):
    raw = sys.stdin.read() if args.proposal == "-" else open(args.proposal).read()
    proposal = _build_proposal(json.loads(raw))

    gate = _make_gate(broker)
    decision = gate.evaluate(proposal, broker.account_state())
    broker.journal.record(
        "gate.decision",
        proposal=json.loads(raw),
        decision=decision.to_dict(),
        dry_run=args.dry_run,
    )

    result = {
        "approved": decision.approved,
        "decision": decision.to_dict(),
        "placed": None,
    }
    if decision.approved and not args.dry_run:
        from datetime import date

        from .manage import PositionMeta, load_meta, save_meta

        result["placed"] = broker.place_bracket_order(proposal.to_bracket())
        meta = load_meta(_meta_path())
        meta[proposal.symbol.upper()] = PositionMeta(
            symbol=proposal.symbol.upper(),
            placed_date=date.today().isoformat(),
            entry_limit=proposal.entry_limit,
            stop_loss=proposal.stop_loss,
            take_profit=proposal.take_profit,
            rationale=proposal.rationale,
            strategy=proposal.strategy,
        )
        save_meta(_meta_path(), meta)
    return result


def _smoke_test(broker: Broker) -> dict:
    """Phase 0 exit test: read account, quote, place a tiny far-from-market
    bracket that cannot fill, confirm it's working, cancel it."""
    steps = {}
    steps["account"] = broker.get_account()
    steps["positions_before"] = broker.get_positions()
    quote = broker.get_quote("AAPL")
    steps["quote"] = quote

    ref = quote.get("last") or quote.get("close")
    if not ref:
        raise RuntimeError("no reference price available for AAPL; is the gateway logged in?")

    # Entry limit 30% below market: guaranteed not to fill during the test.
    entry = round(ref * 0.70, 2)
    placed = broker.place_bracket_order(
        BracketProposal(
            symbol="AAPL", action="BUY", quantity=1,
            entry_limit=entry,
            stop_loss=round(entry * 0.95, 2),
            take_profit=round(entry * 1.10, 2),
        )
    )
    steps["bracket_placed"] = placed

    entry_id = placed["orders"][0]["order_id"]
    steps["cancelled"] = broker.cancel_order(entry_id)
    steps["positions_after"] = broker.get_positions()
    steps["verdict"] = "PHASE 0 SMOKE TEST PASSED"
    return steps
