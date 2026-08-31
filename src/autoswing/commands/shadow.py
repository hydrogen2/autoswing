"""Shadow-book CLI commands: the v2 news-catalyst book and the wide-PEAD
measurement ledger. Virtual positions only — nothing here places orders."""

import json
import sys

from ..broker import Broker
from ..journal import Journal


def _shadow_paths(wide: bool = False):
    from ..config import PROJECT_ROOT
    d = PROJECT_ROOT / "state" / "shadow"
    if wide:
        return d / "wide_positions.json", d / "wide_ledger.jsonl"
    return d / "positions.json", d / "ledger.jsonl"


def _shadow_propose(broker: Broker, args):
    """Full gate evaluation, virtual position on approval. NEVER places.

    --wide: the wide-PEAD measurement book. The gate still runs in full so
    the journal keeps every would-be verdict, but capacity-class failures
    (CAPACITY_RULES) do not block the virtual entry, and quantity is
    standardized to WIDE_NOTIONAL so the series stays comparable across
    live sizing changes."""
    from datetime import date

    from ..shadow import (
        WIDE_NOTIONAL, ShadowPosition, load_book, save_book, waived_rules,
    )
    from .trading import _build_proposal, _make_gate

    wide = bool(getattr(args, "wide", False))
    raw = sys.stdin.read() if args.proposal == "-" else open(args.proposal).read()
    payload = json.loads(raw)
    payload.setdefault("strategy", "pead-wide" if wide else "news-v2")
    proposal = _build_proposal(payload)
    if wide:
        proposal.quantity = max(1, int(WIDE_NOTIONAL // proposal.entry_limit))

    gate = _make_gate(broker)
    decision = gate.evaluate(proposal, broker.account_state())

    waived = waived_rules(wide)
    blocking_failures = [r.rule for r in decision.rules
                         if not r.passed and r.rule not in waived]
    capacity_failures = [r.rule for r in decision.rules
                         if not r.passed and r.rule in waived]
    accepted = not blocking_failures

    entry_price = None
    opened = False
    if accepted:
        quote = broker.get_quote(proposal.symbol)
        entry_price = quote.get("last") or quote.get("close") or proposal.entry_limit
        pos_path, _ = _shadow_paths(wide=wide)
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
        "wide": wide,
        "approved": accepted,
        "opened_virtual": opened,
        "entry_price": entry_price,
        "decision": decision.to_dict(),
    }
    result["capacity_failures_informational"] = capacity_failures
    broker.journal.record("shadow.proposal", proposal=payload, result={
        "wide": wide,
        "approved": accepted, "opened_virtual": opened,
        "entry_price": entry_price,
        "failed_rules": blocking_failures,
        "capacity_failures_informational": capacity_failures,
    })
    return result


def _shadow_mark(config, journal: Journal):
    from datetime import date

    from ..data.prices import fetch_history
    from ..shadow import load_book, mark_position, save_book

    max_hold = int(config.strategy.get("max_hold_days", 15))
    bench = config.strategy.get("benchmark_symbol", "VOO")
    out = {}
    for label, wide in (("v2", False), ("wide", True)):
        pos_path, ledger_path = _shadow_paths(wide=wide)
        book = load_book(pos_path)
        if not book:
            out[label] = {"open": 0, "closed_today": []}
            continue

        history = fetch_history(sorted(book) + [bench], period="3mo")
        bench_df = history.get(bench)
        closed = []
        for sym in list(book):
            df = history.get(sym)
            if df is None:
                continue  # no data today; try again tomorrow
            event = mark_position(book[sym], df, date.today(), max_hold)
            if event:
                event["alpha_pct"] = _alpha(event, bench_df)
                ledger_path.parent.mkdir(parents=True, exist_ok=True)
                with open(ledger_path, "a") as f:
                    f.write(json.dumps(event) + "\n")
                journal.record("shadow.close", book=label,
                               **{k: v for k, v in event.items()
                                  if k != "event"})
                closed.append(event)
                del book[sym]
        save_book(pos_path, book)
        out[label] = {"open": len(book), "closed_today": closed}
    return out


def _alpha(event: dict, bench_df) -> float | None:
    """Trade return minus benchmark return over the same hold (opened ->
    closed dates, close-to-close). None when the series is unavailable —
    a benchmark outage must never fabricate an alpha figure."""
    from datetime import date

    if bench_df is None or not event.get("entry_price"):
        return None
    try:
        def close_on(d: str):
            day = date.fromisoformat(d)
            rows = bench_df[[ts.date() <= day for ts in bench_df.index]]
            return float(rows["Close"].iloc[-1]) if len(rows) else None
        b0, b1 = close_on(event["opened"]), close_on(event["closed"])
        if not b0 or not b1:
            return None
        trade_ret = 100 * (event["exit_price"] / event["entry_price"] - 1)
        return round(trade_ret - 100 * (b1 / b0 - 1), 2)
    except Exception:
        return None


def _shadow_status():
    from ..shadow import ledger_stats, load_book

    out = {}
    for label, wide in (("v2", False), ("wide", True)):
        pos_path, ledger_path = _shadow_paths(wide=wide)
        book = load_book(pos_path)
        out[label] = {
            "open": [p.__dict__ for p in book.values()],
            "ledger": ledger_stats(ledger_path),
        }
    return out
