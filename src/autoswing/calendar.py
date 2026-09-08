"""NYSE trading calendar — the enforcing layer's answer to "is the market open?"

Added 2026-09-08 after Labor Day (09-07) exposed the gap: the gate's
market_hours rule was clock-only, so it PASSED on every probe from 09:45 to
15:45 ET on a fully closed market. Nothing bad happened because the brain
knew the date — but that inverts the project's core principle. Refusal
belongs in code, not in an LLM's recollection.

Deliberately a static table rather than a calendar dependency: it is small,
auditable, offline, and testable. The cost is that it EXPIRES, and a silently
expired table is exactly this codebase's most-repeated bug family (a failure
that renders as a benign state — every day would look like a trading day).
So coverage is explicit and the gate FAILS CLOSED past it: better to refuse
entries until someone extends the table than to trade a calendar we no
longer know. Extending it is a one-line-per-year edit.
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Last date this table is known-good for. Past this, is_trading_day() raises
# and the gate refuses rather than guessing.
COVERAGE_THROUGH = date(2027, 12, 31)

# Full closures (NYSE). Sources: NYSE published holiday calendars.
FULL_CLOSURES: frozenset[date] = frozenset({
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    # 2027
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
})

# Early closes: the session ends 13:00 ET. A full-closure list alone would
# still have let a 15:00 entry through on these days — the same bug wearing
# a different hat. Next one is 2026-11-27, the day after Thanksgiving.
EARLY_CLOSES: frozenset[date] = frozenset({
    date(2026, 11, 27), date(2026, 12, 24),
    date(2027, 11, 26),
})

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)


class CalendarExpired(RuntimeError):
    """Raised past COVERAGE_THROUGH so the caller fails closed, loudly."""


def _guard(d: date) -> None:
    if d > COVERAGE_THROUGH:
        raise CalendarExpired(
            f"trading calendar covers dates through {COVERAGE_THROUGH} but was "
            f"asked about {d}. Extend FULL_CLOSURES/EARLY_CLOSES in "
            f"autoswing/calendar.py. Refusing rather than assuming the market "
            f"is open."
        )


def is_trading_day(d: date) -> bool:
    _guard(d)
    return d.weekday() < 5 and d not in FULL_CLOSURES


def session_close(d: date) -> time:
    """Closing time for a trading day (13:00 ET on early-close days)."""
    _guard(d)
    return EARLY_CLOSE if d in EARLY_CLOSES else REGULAR_CLOSE


def is_market_open(now_et: datetime) -> tuple[bool, str]:
    """(open?, human-readable reason). Reason is surfaced in the gate detail
    so a refusal never reads as a mysterious rejection."""
    d = now_et.date()
    if not is_trading_day(d):
        why = "weekend" if d.weekday() >= 5 else "market holiday"
        return False, f"{d} is a {why}"
    close = session_close(d)
    t = now_et.time()
    if t < REGULAR_OPEN:
        return False, f"pre-open (session {REGULAR_OPEN:%H:%M}-{close:%H:%M} ET)"
    if t >= close:
        early = " (EARLY CLOSE)" if close == EARLY_CLOSE else ""
        return False, f"after close{early} (session ended {close:%H:%M} ET)"
    return True, f"regular session (closes {close:%H:%M} ET)"
