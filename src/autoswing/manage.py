"""Deterministic position management: time-box and pre-earnings exits.

These exits are code, not LLM judgment — the two rules that most protect
the account (never hold through a print, never let capital rot) must fire
even if the brain is down, confused, or eloquent about why "this one is
different".
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path


@dataclass
class PositionMeta:
    symbol: str
    placed_date: str          # YYYY-MM-DD
    entry_limit: float
    stop_loss: float
    take_profit: float
    rationale: str = ""


def load_meta(path: Path) -> dict[str, PositionMeta]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {sym: PositionMeta(**m) for sym, m in raw.items()}


def save_meta(path: Path, meta: dict[str, PositionMeta]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({s: asdict(m) for s, m in meta.items()}, indent=2))
    tmp.replace(path)


def trading_days_between(start: date, end: date) -> int:
    """Weekday count in (start, end]; no holiday calendar (conservative:
    holidays count toward the time-box, never extend it)."""
    if end <= start:
        return 0
    days, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def evaluate_position(
    symbol: str,
    placed_date: str,
    next_earnings: str,       # YYYY-MM-DD | none | unknown
    strategy_cfg: dict,
    today: date | None = None,
) -> tuple[str, str]:
    """Returns (action, detail). Actions: hold | exit_timebox | exit_earnings.

    An 'unknown' earnings date on an OPEN position forces an exit: if we
    cannot prove there is no imminent print, we do not stay exposed to one.
    """
    today = today or date.today()
    max_hold = int(strategy_cfg.get("max_hold_days", 15))
    buffer_days = int(strategy_cfg.get("exit_before_earnings_days", 2))

    held = trading_days_between(date.fromisoformat(placed_date), today)
    if held >= max_hold:
        return "exit_timebox", f"held {held} trading days >= time-box {max_hold}"

    if next_earnings == "unknown" or not next_earnings:
        return "exit_earnings", "next earnings date unverifiable — refusing gap exposure"
    if next_earnings != "none":
        try:
            earnings = date.fromisoformat(next_earnings)
        except ValueError:
            return "exit_earnings", f"unparseable earnings date {next_earnings!r}"
        days_until = (earnings - today).days
        placed = date.fromisoformat(placed_date)
        # A print dated on/before our entry, or already in the past, is
        # behind us — not an upcoming event to avoid. Same-day PEAD entries
        # buy the post-print reaction; stale feeds then leave next_earnings
        # pinned to that just-reported date instead of rolling it a quarter
        # forward, so a blind "earnings 0d away" wrongly flattens a fresh,
        # thesis-intact position (2026-07-21 MMM). The pre-earnings exit must
        # only fire on a print that is still ahead of the position.
        behind_us = earnings <= placed or earnings < today
        if not behind_us and days_until <= buffer_days:
            return "exit_earnings", (
                f"earnings {next_earnings} is {days_until}d away (buffer {buffer_days}d)"
            )

    return "hold", f"held {held}d of {max_hold}, earnings {next_earnings}"
