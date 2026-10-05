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

The ENTRY day is special: the position opens mid-session, so that bar's
extremes include pre-entry action that cannot be ordered against the entry
(2026-09-09: wide ASO was "stopped" by a low printed before the live mirror
entry even existed). The close is the one print a daily bar guarantees came
after the entry, and the entry is inside the bracket — so a day-0 close at
or through a bracket level proves that level was crossed post-entry. Day-0
therefore fills only on the CLOSE crossing a level; an intraday touch that
closes back inside the bracket holds until the next session.

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
    entry_at_open: bool = False,
) -> dict | None:
    """Returns a close event dict, or None if the position stays open.

    Bars strictly BEFORE the open date are ignored. Stop-first on ambiguous
    bars (see module docstring).

    entry_at_open=True is for a position opened AT the session open (the
    backtest): then the whole first bar is post-entry and gets the normal
    full-bar checks. The close-only entry-day rule exists for MID-SESSION
    entries; applied to an open entry it would ignore real first-day
    stop-outs and flatter the result.
    """
    opened = date.fromisoformat(pos.opened)
    for ts in df.index:
        d = ts.date()
        if d < opened or d > today:
            continue
        bar = df.loc[ts]
        if d == opened and not entry_at_open:
            # Mid-session entry: the bar's extremes can't be ordered
            # against the entry, only the close is provably post-entry
            # (see module docstring). Stop-first ordering preserved.
            if float(bar["Close"]) <= pos.stop_loss:
                return _close(pos, d, pos.stop_loss, "stop")
            if float(bar["Close"]) >= pos.take_profit:
                return _close(pos, d, pos.take_profit, "target")
            continue
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


# --- pre-registered news-v2 verdict -------------------------------------------
# Proposed by the manager 2026-09-25 at 11 closed (2W/9L, -$551), approved by
# the owner 2026-09-29 at 12 closed (2W/10L, -$927). Registered MID-SAMPLE on
# purpose: criteria written before the result is known cannot be bent toward
# it, in either direction -- no premature binning on a losing streak, and no
# "let's give it a few more" when the 25th close disappoints.
#
# The rule, verbatim from the proposal:
#   at 25 closed, bin the strategy if avg alpha < 0 or total P&L < 0;
#   anything better earns a promotion DISCUSSION only if it survives
#   dropping the best trade.
#
# Two definitions the proposal left open, fixed here so they cannot be chosen
# after the fact:
#   - The sample is the FIRST 25 closes in ledger order. Scoring "all closes
#     so far" would let a disappointing verdict be re-read later, once a few
#     more trades had moved the number -- renegotiation by waiting.
#   - "Dropping the best trade" removes each criterion's own best: the top-P&L
#     trade for the P&L test, the top-alpha trade for the alpha test. That is
#     the stricter reading; one outlier cannot carry either leg.
# Changing any of this is a dated, deliberate edit that says so -- never a
# quiet one.
V2_VERDICT_N = 25
V2_VERDICT_REGISTERED = "2026-09-29"
V2_VERDICT_RULE = ("at 25 closed: bin if avg alpha < 0 or total P&L < 0; "
                   "otherwise a promotion discussion only if both criteria "
                   "still hold with each one's best trade removed")


def load_ledger(ledger_path: Path) -> list[dict]:
    if not ledger_path.exists():
        return []
    return [json.loads(l) for l in ledger_path.read_text().splitlines() if l.strip()]


def _pnl_alpha(rows: list[dict]) -> tuple[float, float | None, int]:
    total = round(sum(r["pnl"] for r in rows), 2)
    alphas = [r["alpha_pct"] for r in rows
              if isinstance(r.get("alpha_pct"), (int, float))]
    avg = round(sum(alphas) / len(alphas), 3) if alphas else None
    return total, avg, len(alphas)


def _without_best(rows: list[dict], key) -> list[dict]:
    if not rows:
        return rows
    best = max(range(len(rows)), key=lambda i: key(rows[i]))
    return rows[:best] + rows[best + 1:]


def v2_verdict(rows: list[dict]) -> dict:
    """The pre-registered verdict. Below 25 closed it reports progress only;
    the numbers are shown so the trajectory is visible, but they are labelled
    as not-a-result."""
    sample = rows[:V2_VERDICT_N]
    n = len(sample)
    total, avg, alpha_n = _pnl_alpha(sample)
    out = {
        "rule": V2_VERDICT_RULE,
        "registered": V2_VERDICT_REGISTERED,
        "required_n": V2_VERDICT_N,
        "sample_n": n,
        "closed_total": len(rows),
        "total_pnl": total,
        "avg_alpha_pct": avg,
        "alpha_n": alpha_n,
        "alpha_incomplete": alpha_n < n,
    }
    if n < V2_VERDICT_N:
        out.update(verdict="pending", remaining=V2_VERDICT_N - n,
                   note="progress, not a result: no verdict before "
                        f"{V2_VERDICT_N} closed")
        return out
    if avg is None:
        # Every row lacks alpha: the alpha criterion cannot be evaluated, and
        # a missing leg is never read as a passing one.
        out.update(verdict="undeterminable",
                   reasons=["no alpha on any row in the sample"])
        return out

    reasons = []
    if avg < 0:
        reasons.append(f"avg alpha {avg}% < 0")
    if total < 0:
        reasons.append(f"total P&L {total} < 0")
    if reasons:
        out.update(verdict="bin", reasons=reasons)
        return out

    pnl_ex_best, _, _ = _pnl_alpha(_without_best(sample, lambda r: r["pnl"]))
    alpha_rows = [r for r in sample if isinstance(r.get("alpha_pct"), (int, float))]
    _, alpha_ex_best, _ = _pnl_alpha(
        _without_best(alpha_rows, lambda r: r["alpha_pct"]))
    out.update(total_pnl_ex_best=pnl_ex_best,
               avg_alpha_pct_ex_best=alpha_ex_best)
    fragile = []
    if pnl_ex_best < 0:
        fragile.append(f"total P&L without best trade {pnl_ex_best} < 0")
    if alpha_ex_best is None or alpha_ex_best < 0:
        fragile.append(f"avg alpha without best trade {alpha_ex_best} < 0")
    if fragile:
        # Not binned by the rule, but it does not earn a promotion discussion
        # either: the result rests on one trade. What happens next is the
        # owner's call, and the rule does not pretend otherwise.
        out.update(verdict="no_promotion_rests_on_one_trade", reasons=fragile)
    else:
        out.update(verdict="promotion_discussion",
                   reasons=["both criteria hold with each best trade removed"])
    return out


# --- verdict alert (owner ruling 2026-09-29) ---------------------------------
# "Alert me before dropping it, I may want to keep it longer." Nothing in the
# system stops the v2 book on its own -- the verdict only reports -- and that
# is deliberate: retiring it is the owner's decision. This makes sure the
# owner hears about the verdict directly, ONCE, rather than finding it as one
# line in a routine report.

def verdict_alert(verdict: dict, already_alerted: dict | None) -> str | None:
    """The alert body when the verdict is new and non-pending, else None.

    Alerts once per distinct verdict: a flip from pending, or a later change
    (which should only happen if the ledger itself is corrected)."""
    v = verdict.get("verdict")
    if v in (None, "pending"):
        return None
    if already_alerted and already_alerted.get("verdict") == v:
        return None

    lines = [
        f"news-v2 shadow book: pre-registered verdict = {v.upper()}",
        "",
        "NOTHING HAS BEEN STOPPED. The v2 shadow book keeps running until you",
        "rule; no agent is permitted to retire, pause, or alter it.",
        "",
        f"Rule (registered {verdict.get('registered')}): {verdict.get('rule')}",
        "",
        f"First {verdict.get('sample_n')} closes: total P&L {verdict.get('total_pnl')}, "
        f"avg alpha {verdict.get('avg_alpha_pct')}% (alpha on "
        f"{verdict.get('alpha_n')}/{verdict.get('sample_n')} rows).",
    ]
    if "total_pnl_ex_best" in verdict:
        lines.append(f"Without best trade: P&L {verdict['total_pnl_ex_best']}, "
                     f"avg alpha {verdict.get('avg_alpha_pct_ex_best')}%.")
    for r in verdict.get("reasons", []):
        lines.append(f"  - {r}")
    lines += [
        "",
        "Your options:",
        "  (a) Retire the book as the rule says.",
        "  (b) Keep it running longer. That is your call and costs nothing,",
        "      but the verdict on the first 25 stands as recorded -- trades",
        "      after #25 are a NEW, out-of-sample period, not a rescue of the",
        "      old one. So that 'longer' has an end, set a fresh pre-registered",
        "      check for it now (e.g. the next 25 closes, same thresholds).",
        "",
        f"Closed so far in total: {verdict.get('closed_total')}.",
    ]
    return "\n".join(lines) + "\n"
