"""Earnings calendar via Nasdaq's public API (free, no key) and
per-symbol next-report lookup via yfinance.

Data honesty rule: when a source doesn't know, we say "unknown" — never
guess. The risk gate treats "unknown" as a rejection, which is the point.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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
