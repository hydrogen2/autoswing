"""Command-line tool surface. Every command prints a single JSON document.

This is what the scheduled agent calls; humans can run the same commands
to see exactly what the agent sees. Handlers live in commands/ (trading,
shadow, research); this module owns the argparse surface, routing, the
watchdog, and the JSON envelope.
"""

import argparse
import json
import sys

from .broker import Broker
from .config import load_config
from .journal import Journal

# Commands that never touch the broker connection.
DATA_COMMANDS = (
    "scan-candidates", "next-earnings", "scan-movers",
    "shadow-mark", "shadow-status", "scan-upcoming",
    "forecast-log", "forecast-score", "forecast-stats",
    "exit-counterfactuals", "log-skip", "skip-outcomes", "fill-quality",
    "backtest", "lesson-pending", "lesson-log", "lessons", "trim-compare",
)


def _build_parser() -> argparse.ArgumentParser:
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
    sp.add_argument(
        "--wide", action="store_true",
        help="wide-PEAD measurement ledger: capacity gate rules become "
        "informational, quantity standardized to a fixed notional, "
        "separate book/ledger. Strategy-definition rules still block.",
    )

    sub.add_parser(
        "shadow-mark",
        help="shadow books: mark virtual positions vs real prices, close on "
        "stop/target/timebox (preclose task; covers v2 and wide-PEAD)",
    )

    sub.add_parser("shadow-status",
                   help="shadow books: open positions + ledger stats "
                   "(v2 and wide-PEAD)")

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

    sub.add_parser(
        "fill-quality",
        help="research: fill prices vs approved entry/stop/target prices "
        "across the journal — the paper-to-live slippage baseline",
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

    bt = sub.add_parser(
        "backtest",
        help="research: replay the mechanical PEAD skeleton over historical "
        "earnings (heavy on first run; caches to state/backtest). Results "
        "carry survivorship/timing/judgment caveats — never quote as "
        "expected live performance.",
    )
    bt.add_argument("--start", required=True, help="YYYY-MM-DD")
    bt.add_argument("--end", required=True, help="YYYY-MM-DD")
    bt.add_argument("--min-move", type=float, default=None,
                    help="reaction move floor, pct (default 5.0)")
    bt.add_argument("--min-volume-ratio", type=float, default=None,
                    help="reaction volume ratio floor (default 2.0)")
    bt.add_argument("--min-surprise", type=float, default=None,
                    help="EPS surprise floor, pct (default 5.0)")

    tc = sub.add_parser(
        "trim-compare",
        help="research: concentrated-position trim rules vs BUY-AND-HOLD of "
        "the same name (return AND drawdown). Measurement only — no orders.",
    )
    tc.add_argument("symbols", help="comma-separated, e.g. CRDO,RKLB,MU,VRT")
    tc.add_argument("--days", type=int, default=730)
    tc.add_argument("--shares", type=int, default=100)

    sub.add_parser(
        "lesson-pending",
        help="reflection memory: closed trades with no lesson yet, with "
        "realized R / return / alpha vs benchmark (preclose task)",
    )
    ll = sub.add_parser(
        "lesson-log",
        help="reflection memory: record an immutable 2-4 sentence lesson "
        "for a closed trade (JSON: symbol, closed_date, thesis_held, lesson)",
    )
    ll.add_argument("lesson", help="lesson JSON path, or '-' for stdin")
    lz = sub.add_parser(
        "lessons",
        help="reflection memory: past lessons formatted for the entry window "
        "(same-symbol first when --symbol given)",
    )
    lz.add_argument("--symbol", default=None)

    return parser


def main() -> None:
    args = _build_parser().parse_args()
    config = load_config()
    journal = Journal(config.journal_dir)
    _arm_watchdog(journal, args.command)

    try:
        if args.command == "journal-note":
            result = journal.record("brain.note", note=_resolve_note(args.note))
        elif args.command in DATA_COMMANDS:
            from .commands.research import _dispatch_data

            result = _dispatch_data(config, journal, args)
        else:
            from .commands.trading import _dispatch

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

    # backtest legitimately runs long on a cold cache (~1 calendar request
    # per trading day + price cohorts); everything else keeps the tight wall.
    default = "3600" if command == "backtest" else "180"
    limit = int(os.environ.get("AUTOSWING_CMD_TIMEOUT", default))

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
