"""Shadow book: virtual execution for candidate strategies.

Two books share this machinery:

- v2 news-catalyst (state/shadow/positions.json): a shadow proposal runs
  through the REAL risk gate (so the record includes would-be gate
  verdicts) but never places an order. Portfolio-level caps are recorded
  and waived (see PORTFOLIO_RULES); per-position sizing still binds.
- wide-PEAD measurement (state/shadow/wide_positions.json): every
  mechanically-qualifying PEAD candidate — including ones entered live and
  ones blocked purely by capacity — logged at a standardized notional.
  Capacity-class gate rules (CAPACITY_RULES) are recorded but do not block;
  strategy-definition rules still do. Purpose: accrue strategy-edge sample
  size decoupled from the account's capital constraints.

Approved proposals open a virtual position; a daily mark closes them
against real subsequent prices using the same bracket + time-box rules the
live book uses.

Fill model (documented conservatism): marks use daily bars from the session
of entry onward. When a bar's low breaches the stop AND its high reaches the
target, the STOP is assumed to fill first. Intraday ordering is unknowable
from daily bars; resolving ties against the strategy means shadow results
understate rather than flatter. Entry price is the delayed quote at proposal
time (falls back to the entry limit).

Promotion decision (owner): compare the shadow ledger's realized stats
against the live PEAD ledger after the shadow season. This module never
touches the broker.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from .manage import trading_days_between

# Wide-PEAD ledger: fixed virtual notional per position. Deliberately NOT
# derived from live sizing config — the measurement series must stay
# comparable across live sizing changes (15%->10% on 2026-08-05 would have
# silently rescaled it).
WIDE_NOTIONAL = 5000.0

# Gate rules that reflect the account's capacity/state rather than the
# strategy's definition. Split in two, because the two halves bias a
# virtual book differently (2026-08-31):
#
# PORTFOLIO_RULES depend on the LIVE book's current state, so leaving them
# in place makes a virtual book record entries only on days the real book
# happened to have room — the v2 ledger was rejecting genuine candidates
# (TENB, NEO on 08-28) purely because live was 10/10. Both shadow books
# bypass these; a failure is recorded, never blocking.
#
# POSITION_RULES depend only on the proposal and virtual equity, not on the
# live book, so they introduce no such bias and represent sizing discipline
# any promoted strategy would still have to meet. v2 KEEPS them. --wide
# bypasses them too, because it overwrites quantity with a standardized
# notional, which makes per-position sizing checks meaningless there.
#
# Everything else (bracket_structure, market_hours, earnings_blackout,
# liquidity, min_price, short_selling, kill_switch) always blocks.
PORTFOLIO_RULES = frozenset({
    "daily_loss_halt", "max_open_positions", "max_gross_exposure",
    "duplicate_position", "core_overlap", "pdt_guard",
})
POSITION_RULES = frozenset({"risk_per_trade", "max_position_size"})
CAPACITY_RULES = PORTFOLIO_RULES | POSITION_RULES


def waived_rules(wide: bool) -> frozenset:
    """Rules recorded-but-not-blocking for a shadow proposal."""
    return CAPACITY_RULES if wide else PORTFOLIO_RULES


@dataclass
class ShadowPosition:
    symbol: str
    strategy: str            # e.g. news-v2
    opened: str              # YYYY-MM-DD
    entry_price: float
    quantity: int
    stop_loss: float
    take_profit: float
    rationale: str = ""


def load_book(path: Path) -> dict[str, ShadowPosition]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {sym: ShadowPosition(**p) for sym, p in raw.items()}


def save_book(path: Path, book: dict[str, ShadowPosition]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({s: asdict(p) for s, p in book.items()}, indent=2))
    tmp.replace(path)


def mark_position(
    pos: ShadowPosition,
    df,                       # OHLCV DataFrame (daily bars)
    today: date,
    max_hold_days: int,
) -> dict | None:
    """Returns a close event dict, or None if the position stays open.

    Bars strictly BEFORE the open date are ignored. Stop-first on ambiguous
    bars (see module docstring).
    """
    opened = date.fromisoformat(pos.opened)
    for ts in df.index:
        d = ts.date()
        if d < opened or d > today:
            continue
        bar = df.loc[ts]
        if float(bar["Low"]) <= pos.stop_loss:
            return _close(pos, d, pos.stop_loss, "stop")
        if float(bar["High"]) >= pos.take_profit:
            return _close(pos, d, pos.take_profit, "target")
        if trading_days_between(opened, d) >= max_hold_days:
            return _close(pos, d, float(bar["Close"]), "timebox")
    return None


def _close(pos: ShadowPosition, on: date, price: float, reason: str) -> dict:
    pnl = round((price - pos.entry_price) * pos.quantity, 2)
    return {
        "event": "shadow.close",
        "symbol": pos.symbol,
        "strategy": pos.strategy,
        "opened": pos.opened,
        "closed": on.isoformat(),
        "entry_price": pos.entry_price,
        "exit_price": round(price, 4),
        "quantity": pos.quantity,
        "reason": reason,
        "pnl": pnl,
        "days_held": trading_days_between(date.fromisoformat(pos.opened), on),
    }


def ledger_stats(ledger_path: Path) -> dict:
    if not ledger_path.exists():
        return {"closed": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}
    closed = wins = losses = 0
    total = 0.0
    alphas = []
    for line in ledger_path.read_text().splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        closed += 1
        total += e["pnl"]
        if e["pnl"] > 0:
            wins += 1
        else:
            losses += 1
        if isinstance(e.get("alpha_pct"), (int, float)):
            alphas.append(e["alpha_pct"])
    return {"closed": closed, "wins": wins, "losses": losses,
            "total_pnl": round(total, 2),
            "avg_alpha_pct": round(sum(alphas) / len(alphas), 2) if alphas else None,
            "alpha_n": len(alphas)}
