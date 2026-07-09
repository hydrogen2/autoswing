"""Earnings calendar via Nasdaq's public API (free, no key) and
per-symbol next-report lookup via yfinance.

Data honesty rule: when a source doesn't know, we say "unknown" — never
guess. The risk gate treats "unknown" as a rejection, which is the point.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

import requests

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
    """'$0.71' / '($0.30)' / '$3,182,376,227' -> float; '' -> None."""
    if not s:
        return None
    neg = "(" in s
    cleaned = re.sub(r"[^0-9.]", "", s)
    if not cleaned:
        return None
    value = float(cleaned)
    return -value if neg else value


def parse_calendar_rows(rows: list[dict], day: date) -> list[Report]:
    reports = []
    for r in rows:
        symbol = (r.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        surprise = r.get("surprise")
        reports.append(
            Report(
                symbol=symbol,
                report_date=day.isoformat(),
                timing=TIMING.get(r.get("time"), "unknown"),
                eps_actual=_money(r.get("eps")),
                eps_forecast=_money(r.get("epsForecast")),
                surprise_pct=float(surprise) if surprise not in (None, "", "N/A") else None,
                num_estimates=int(r["noOfEsts"]) if r.get("noOfEsts") else None,
                market_cap=_money(r.get("marketCap")),
                company=r.get("name", ""),
            )
        )
    return reports


def fetch_calendar_day(day: date, session: requests.Session | None = None) -> list[Report]:
    s = session or requests.Session()
    resp = s.get(
        NASDAQ_URL, params={"date": day.isoformat()}, headers=HEADERS, timeout=20
    )
    resp.raise_for_status()
    rows = ((resp.json().get("data") or {}).get("rows")) or []
    return parse_calendar_rows(rows, day)


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

    today = date.today()
    ticker = yf.Ticker(symbol)
    known: list[date] = []

    try:
        known.extend(ticker.calendar.get("Earnings Date") or [])
    except Exception:
        pass
    try:
        df = ticker.get_earnings_dates(limit=8)
        if df is not None:
            known.extend(d.date() for d in df.index)
    except Exception:
        pass

    future = sorted(d for d in known if d >= today)
    if future:
        return future[0].isoformat()
    past = [d for d in known if d < today]
    if past and (today - max(past)).days <= 30:
        return "none"  # just reported; next report is a quarter away
    return "unknown"
