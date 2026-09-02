"""Broker-backed CLI commands: orders, risk gate, position management,
reconciliation, benchmark. Everything here holds a live Broker connection."""

import json
import sys

from ..broker import Broker, BracketProposal


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
        status = gate.status(broker.account_state())
        status["sizing_caps"] = _sizing_caps(gate.cfg, status["virtual_equity"])
        return status
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
        from .shadow import _shadow_propose

        return _shadow_propose(broker, args)
    if args.command == "benchmark-mark":
        return _benchmark_mark(broker)
    raise ValueError(f"unknown command {args.command!r}")


def _meta_path():
    from ..config import PROJECT_ROOT
    return PROJECT_ROOT / "state" / "positions.json"


def _sizing_caps(risk_cfg: dict, virtual_equity: float) -> dict:
    """Today's sizing limits in dollars, computed the same way the gate's
    evaluate() computes them. The brain sized to a remembered ~15% cap on
    2026-08-14 (ENS) and 2026-08-17 (HTHT) — both rejected on
    max_position_size — so gate-status now states the live numbers."""
    return {
        "risk_per_trade_pct": float(risk_cfg["risk_per_trade_pct"]),
        "risk_budget_dollars": round(
            virtual_equity * float(risk_cfg["risk_per_trade_pct"]) / 100.0, 2),
        "max_position_pct": float(risk_cfg["max_position_pct"]),
        "max_position_notional": round(
            virtual_equity * float(risk_cfg["max_position_pct"]) / 100.0, 2),
        "max_gross_exposure_pct": float(risk_cfg["max_gross_exposure_pct"]),
        "max_gross_exposure_dollars": round(
            virtual_equity * float(risk_cfg["max_gross_exposure_pct"]) / 100.0, 2),
    }


def _make_gate(broker: Broker):
    from ..config import PROJECT_ROOT
    from ..risk_gate import RiskGate

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

    from ..risk_gate import TradeProposal

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

        from ..manage import PositionMeta, load_meta, save_meta

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


def _manage_positions(broker: Broker, enforce: bool, meta_path=None):
    from datetime import date

    from ..data.earnings import next_earnings_date
    from ..manage import PositionMeta, evaluate_position, load_meta, save_meta

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
        # Per-position marks (when the broker snapshot carries them) so the
        # brain can judge drift health per name, not just book-level.
        pos = held.get(sym, {})
        mark, avg = pos.get("market_price"), pos.get("avg_cost")
        entry = {"symbol": sym, "action": action, "detail": detail,
                 "next_earnings": ned, "enforced": False,
                 "mark": mark,
                 "unrealized_pnl": pos.get("unrealized_pnl"),
                 "unrealized_pct": (round(100.0 * (mark - avg) / avg, 2)
                                    if mark and avg else None)}
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

    from ..config import PROJECT_ROOT
    from ..manage import load_meta
    from ..reconcile import (
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

    from ..config import PROJECT_ROOT
    from ..data.prices import fetch_history

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
                existing.append(json.loads(line))
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
            f.write(json.dumps(row) + "\n")
    broker.journal.record("benchmark.mark", result=entry)
    return entry


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
