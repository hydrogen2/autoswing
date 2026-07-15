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

    args = parser.parse_args()
    config = load_config()
    journal = Journal(config.journal_dir)

    try:
        if args.command == "journal-note":
            result = journal.record("brain.note", note=args.note)
        elif args.command in ("scan-candidates", "next-earnings"):
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
    for sym, m in meta.items():
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


def _benchmark_mark(broker: Broker):
    import json as _json
    from datetime import date

    from .config import PROJECT_ROOT
    from .data.prices import fetch_history

    gate = _make_gate(broker)
    status = gate.status(broker.account_state())

    bench_sym = broker.config.strategy.get("benchmark_symbol", "VOO")
    hist = fetch_history([bench_sym], period="5d")
    bench_close = float(hist[bench_sym]["Close"].iloc[-1]) if bench_sym in hist else None

    path = PROJECT_ROOT / "state" / "benchmark.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    first = None
    if path.exists():
        lines = path.read_text().strip().splitlines()
        if lines:
            first = _json.loads(lines[0])

    entry = {
        "date": date.today().isoformat(),
        "virtual_equity": status["virtual_equity"],
        "benchmark_close": bench_close,
        "drawdown_pct": status["drawdown_pct"],
        "kill_tripped": status["kill_tripped"],
    }
    if first and first.get("benchmark_close") and bench_close:
        entry["bot_return_pct"] = round(
            100 * (status["virtual_equity"] / first["virtual_equity"] - 1), 2
        )
        entry["benchmark_return_pct"] = round(
            100 * (bench_close / first["benchmark_close"] - 1), 2
        )
    with open(path, "a") as f:
        f.write(_json.dumps(entry) + "\n")
    broker.journal.record("benchmark.mark", result=entry)
    return entry


def _dispatch_data(config, journal: Journal, args):
    if args.command == "scan-candidates":
        from .data.candidates import scan

        result = scan(config.risk, days_back=args.days_back, min_move_pct=args.min_move)
        journal.record(
            "data.scan_candidates",
            scanned=result["scanned"], passing=result["passing"],
            symbols=[c["symbol"] for c in result["candidates"]],
        )
        return result
    if args.command == "next-earnings":
        from .data.earnings import next_earnings_date

        return {"symbol": args.symbol.upper(),
                "next_earnings_date": next_earnings_date(args.symbol)}
    raise ValueError(f"unknown data command {args.command!r}")


def _make_gate(broker: Broker):
    from .config import PROJECT_ROOT
    from .risk_gate import RiskGate

    return RiskGate(
        risk_config=broker.config.risk,
        state_path=PROJECT_ROOT / "state" / "gate_state.json",
    )


def _propose_trade(broker: Broker, args):
    from .risk_gate import TradeProposal

    raw = sys.stdin.read() if args.proposal == "-" else open(args.proposal).read()
    proposal = TradeProposal(**json.loads(raw))

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
