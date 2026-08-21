"""Earnings calendar via Nasdaq's public API (free, no key) and
per-symbol next-report lookup via yfinance.

Data honesty rule: when a source doesn't know, we say "unknown" — never
guess. The risk gate treats "unknown" as a rejection, which is the point.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")

NASDAQ_URL = "https://api.nasdaq.com/api/calendar/earnings"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Accept": "application/json",
}

TIMING = {
    "time-pre-market": "bmo",     # before market open -> reaction same day
    "time-after-hours": "amc",    # after close -> reaction next trading day
    "time-not-supplied": "unknown",
}


@dataclass
class Report:
    symbol: str
    report_date: str          # YYYY-MM-DD
    timing: str               # bmo | amc | unknown
    eps_actual: float | None
    eps_forecast: float | None
    surprise_pct: float | None
    num_estimates: int | None
    market_cap: float | None
    company: str = ""
    # Populated by parse_calendar_rows; see quality_flags() for the meanings.
    quality_flags: list[str] = field(default_factory=list)


def _money(s: str | None) -> float | None:
    """'$0.71' / '($0.30)' / '-$1.00' / '$3,182,376,227' -> float; '' -> None.

    Nasdaq formats negatives BOTH ways: parens in most rows, but a bare
    minus in some (ECOR 2023-08-09 forecast '-$1.00' parsed as +1.0 until
    2026-08-11, flipping a loss estimate into a beat denominator)."""
    if not s:
        return None
    neg = "(" in s or s.lstrip().startswith("-")
    cleaned = re.sub(r"[^0-9.]", "", s)
    if not cleaned:
        return None
    value = float(cleaned)
    return -value if neg else value


def _number(v) -> float | None:
    """Tolerant float: 'N/A', '', None, or junk -> None. The calendar feed
    uses 'N/A' freely and a missing datum must never crash the scan."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v) -> int | None:
    f = _number(v)
    return int(f) if f is not None else None


# -- data-quality flags --------------------------------------------------------
#
# The calendar feed reports GAAP EPS against a consensus of unknown vintage
# and unknown depth. Three times in a week (2026-08-14 EROC/KTB, 08-17,
# 08-20 DUOT) it produced a surprise_pct that was arithmetically fine and
# economically meaningless, and only the brain's manual news cross-check
# caught it. These flags make the same doubt structural: they never suppress
# a candidate, they label WHY the headline number may not mean what it says.
#
# DUOT 2026-08-20 is the worked example: forecast -$0.02, reported -$0.13
# on ONE estimate -> "-550% miss", while the company actually printed $1.61
# including a $53.2M asset-sale gain and announced a 55MW hosting deal.
# Flags raised: thin_coverage, tiny_denominator, extreme_surprise_ratio.

TINY_DENOMINATOR = 0.10       # |forecast| below this makes the % unstable
EXTREME_SURPRISE_PCT = 200.0  # beyond this the ratio is noise, not signal
SURPRISE_TOLERANCE_PCT = 5.0  # feed's own surprise vs derived, rounding slack


def quality_flags(r: "Report") -> list[str]:
    """Reasons the headline surprise may mislead. Never a rejection —
    the brain decides; this only ensures it cannot be surprised silently."""
    flags = []
    if (r.num_estimates or 0) < 3:
        flags.append("thin_coverage")
    a, f = r.eps_actual, r.eps_forecast
    if a is None or f is None:
        flags.append("incomplete_eps")
        return flags
    if abs(f) < TINY_DENOMINATOR:
        flags.append("tiny_denominator")
    if (a < 0) != (f < 0):
        flags.append("sign_flip")
    if r.surprise_pct is not None:
        if abs(r.surprise_pct) > EXTREME_SURPRISE_PCT:
            flags.append("extreme_surprise_ratio")
        if f != 0:
            derived = 100.0 * (a - f) / abs(f)
            if abs(derived - r.surprise_pct) > SURPRISE_TOLERANCE_PCT:
                flags.append("surprise_inconsistent")
    return flags


def parse_calendar_rows(rows: list[dict], day: date) -> list[Report]:
    reports = []
    for r in rows:
        symbol = (r.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        reports.append(
            Report(
                symbol=symbol,
                report_date=day.isoformat(),
                timing=TIMING.get(r.get("time"), "unknown"),
                eps_actual=_money(r.get("eps")),
                eps_forecast=_money(r.get("epsForecast")),
                surprise_pct=_number(r.get("surprise")),
                num_estimates=_int(r.get("noOfEsts")),
                market_cap=_money(r.get("marketCap")),
                company=r.get("name", ""),
            )
        )
        reports[-1].quality_flags = quality_flags(reports[-1])
    return reports


def fetch_calendar_rows(day: date, session: requests.Session | None = None) -> list[dict]:
    """Raw calendar rows for one day, unparsed. The backtest caches these
    verbatim so parser fixes apply retroactively on reread."""
    s = session or requests.Session()
    resp = s.get(
        NASDAQ_URL, params={"date": day.isoformat()}, headers=HEADERS, timeout=20
    )
    resp.raise_for_status()
    return ((resp.json().get("data") or {}).get("rows")) or []


def fetch_calendar_day(day: date, session: requests.Session | None = None) -> list[Report]:
    return parse_calendar_rows(fetch_calendar_rows(day, session), day)


def recent_reporters(days_back: int, today: date | None = None) -> list[Report]:
    """Every report in the last `days_back` calendar days (weekdays only),
    today included — after-close reporters from yesterday are this
    morning's freshest candidates."""
    today = today or date.today()
    session = requests.Session()
    reports: list[Report] = []
    for offset in range(days_back + 1):
        day = today - timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        reports.extend(fetch_calendar_day(day, session))
    return reports


def next_earnings_date(symbol: str) -> str:
    """Next scheduled report as YYYY-MM-DD, 'none', or 'unknown'.

    'none' is only returned when it can be *derived*: the company reported
    within the last 30 days, so the next quarterly report cannot fall inside
    any sane blackout window. Everything else unverifiable is 'unknown'
    (which the gate rejects) — never a guess.
    """
    import yfinance as yf

    now_et = datetime.now(ET)
    today = now_et.date()
    ticker = yf.Ticker(symbol)
    known: list[date] = []
    stamped: list[datetime] = []   # rows that carry a report time

    try:
        known.extend(ticker.calendar.get("Earnings Date") or [])
    except Exception:
        pass
    try:
        df = ticker.get_earnings_dates(limit=8)
        if df is not None:
            for ts in df.index:
                known.append(ts.date())
                if getattr(ts, "tzinfo", None) is not None:
                    stamped.append(ts.to_pydatetime().astimezone(ET))
    except Exception:
        pass

    return resolve_next_earnings(known, stamped, now_et)


def resolve_next_earnings(known: list[date], stamped: list[datetime],
                          now_et: datetime) -> str:
    """Pure decision over the fetched dates (unit-testable without yfinance).

    Same-day BMO staleness (family instance #7, HTHT 2026-08-17): a report
    dated TODAY that already printed before the open is not "upcoming" —
    treating it as such trips the earnings blackout on legitimate day-0
    PEAD entries. Deterministic test: the row's own timestamp is at/before
    the open and that time has passed. AMC rows stay upcoming until they
    print; unstamped same-day rows stay upcoming (never guess)."""
    today = now_et.date()
    reported_today = {
        ts.date() for ts in stamped
        if ts.date() == today
        and (ts.hour, ts.minute) <= (9, 30)
        and now_et >= ts
    }

    future = sorted(d for d in known if d >= today and d not in reported_today)
    if future:
        return future[0].isoformat()
    past = [d for d in known if d < today or d in reported_today]
    if past and (today - max(past)).days <= 30:
        return "none"  # just reported; next report is a quarter away
    return "unknown"
