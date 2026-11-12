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


def _virtual_entry(broker: Broker, proposal):
    """(price, source, pricing detail) for a virtual fill, either book.

    The broker quote on the paper account is ~15 minutes delayed. A virtual
    fill at that price can be one no real order could have had (DELL 09-03
    and CBRL 09-24: both ran away from a limit priced off it). From
    2026-11-12 both shadow books therefore fill at the fresh price when one
    is available, and record which source was used; when none is, they
    fall back to the broker-quote chain and say so.

    Wide: from the first row of its extras sample (c51039b). v2: by the
    owner's ruling later the same day, with its out-of-sample period at
    2/25 and no v2 position open. Rows 1-2 of that period (CRL, HHH) keep
    their delayed-quote fills; every later row is priced here. Neither
    registered rule (sample, criteria, exit model) changes.
    """
    from ..data.fresh import FRESH, fresh_price, positive_price, reference_quote

    quote = broker.get_quote(proposal.symbol)
    ib_price, ib_source = proposal.entry_limit, "entry_limit"
    for key in ("last", "close"):
        # IB answers -1 for "no data" outside the session; -1.0 is truthy,
        # and it is not a price (guard added 2026-11-12, no row affected).
        value = positive_price(quote.get(key))
        if value is not None:
            ib_price, ib_source = value, f"ib_{key}"
            break

    ref = reference_quote(quote, fresh_price(proposal.symbol))
    pricing = {
        "ib_last": quote.get("last"),
        "ib_quote_basis": ref["ib_quote_basis"],
        "fresh": ref["fresh"],
        "fresh_vs_ib_last_pct": ref["fresh_vs_ib_last_pct"],
        "reference_basis": ref["reference_basis"],
    }
    if ref["reference_basis"] == FRESH:
        return ref["reference_price"], "fresh", pricing
    return ib_price, ib_source, pricing


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
    entry_source = None
    pricing = None
    opened = False
    if accepted:
        entry_price, entry_source, pricing = _virtual_entry(broker, proposal)
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
                next_earnings=str(proposal.next_earnings_date or ""),
            )
            save_book(pos_path, book)
            opened = True

    result = {
        "shadow": True,
        "wide": wide,
        "approved": accepted,
        "opened_virtual": opened,
        "entry_price": entry_price,
        "entry_price_source": entry_source,
        "decision": decision.to_dict(),
    }
    result["capacity_failures_informational"] = capacity_failures
    journaled = {
        "wide": wide,
        "approved": accepted, "opened_virtual": opened,
        "entry_price": entry_price,
        "entry_price_source": entry_source,
        "failed_rules": blocking_failures,
        "capacity_failures_informational": capacity_failures,
    }
    if pricing is not None:
        # Both prices and their ages, so any later read of the ledger can
        # count the rows that opened on a fallback instead of assuming none.
        result["entry_pricing"] = pricing
        journaled["entry_pricing"] = pricing
    broker.journal.record("shadow.proposal", proposal=payload, result=journaled)
    return result


def _shadow_mark(config, journal: Journal):
    from datetime import date

    from ..data.prices import fetch_history
    from ..shadow import load_book, mark_position, save_book

    max_hold = int(config.strategy.get("max_hold_days", 15))
    buffer_days = int(config.strategy.get("exit_before_earnings_days", 2))
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
            event = mark_position(book[sym], df, date.today(), max_hold,
                                  earnings_buffer_days=buffer_days)
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
    from ..shadow import (
        ledger_stats, load_book, load_ledger, v2_oos_verdict, v2_verdict,
    )

    out = {}
    for label, wide in (("v2", False), ("wide", True)):
        pos_path, ledger_path = _shadow_paths(wide=wide)
        book = load_book(pos_path)
        out[label] = {
            "open": [p.__dict__ for p in book.values()],
            "ledger": ledger_stats(ledger_path),
        }
        if label == "v2":
            # Pre-registered 2026-09-29; the report quotes this, it does not
            # recompute or reinterpret it.
            rows = load_ledger(ledger_path)
            out[label]["preregistered_verdict"] = v2_verdict(rows)
            # Post-#25 out-of-sample period (owner kept the book 2026-11-04;
            # registered 2026-11-06). Quoted, never reinterpreted.
            out[label]["oos_preregistered_verdict"] = v2_oos_verdict(rows)
        if label == "wide":
            # Pre-registered 2026-10-14 (owner-approved); same rule: quoted,
            # never reinterpreted.
            from ..config import PROJECT_ROOT
            from ..lessons import extract_closed_trades
            from ..shadow import wide_verdict

            live = {c.id: c for c in extract_closed_trades(PROJECT_ROOT / "journal")}
            wide_rows = load_ledger(ledger_path)
            out[label]["preregistered_verdict"] = wide_verdict(wide_rows, live)
            # Second period (owner kept the ledger 2026-11-12, registered the
            # same day): extras against taken trades, both inside the wide
            # ledger. Quoted, never reinterpreted; the first block is frozen.
            from ..wide_extras import wide_extras_verdict_safe

            out[label]["extras_preregistered_verdict"] = wide_extras_verdict_safe(
                wide_rows, PROJECT_ROOT / "journal")
    # Hedged-book line (registered 2026-11-10): the live book's daily returns
    # with a fixed half-of-equity index short. A measurement riding on this
    # command because it is local-only and already read nightly; it is not a
    # book and nothing acts on it.
    from ..config import PROJECT_ROOT
    from ..hedge import hedged_book_safe

    out["hedged_book"] = hedged_book_safe(
        PROJECT_ROOT / "state" / "benchmark.jsonl")
    return out
