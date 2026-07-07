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

    args = parser.parse_args()
    config = load_config()
    journal = Journal(config.journal_dir)

    try:
        with Broker(config, journal) as broker:
            result = _dispatch(broker, args)
    except Exception as e:
        journal.record("cli.error", command=args.command, error=str(e))
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)

    print(json.dumps({"ok": True, "result": result}, indent=2, default=str))


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
    raise ValueError(f"unknown command {args.command!r}")


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
