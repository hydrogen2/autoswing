"""Movers scan for the v2 news-catalyst shadow strategy.

Surfaces big price+volume movers that are NOT explained by a recent
earnings report — those belong to PEAD. The brain identifies the actual
catalyst (FDA, M&A fallout, guidance, contract, upgrade) via news search;
this module only finds "something happened here" candidates.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .earnings import recent_reporters
from .prices import (
    FULL_SESSION, PARTIAL_SESSION, V2_VOLUME_CONFIRM_RATIO, fetch_history,
    session_complete, volume_verdict,
)


def _screen_symbols() -> list[str]:
    """Yahoo's predefined screeners via yfinance; tolerant of API drift."""
    import yfinance as yf

    symbols: list[str] = []
    for name in ("day_gainers", "most_actives"):
        try:
            res = yf.screen(name, count=50)
            quotes = res.get("quotes", []) if isinstance(res, dict) else []
            symbols.extend(
                q.get("symbol") for q in quotes if q.get("symbol")
            )
        except Exception:
            continue
    # Dedupe, drop non-plain-equity tickers (units, warrants, dots).
    out = []
    for s in symbols:
        if s and s.isalpha() and s.upper() == s and s not in out:
            out.append(s)
    return out


def _reported_recently(symbol: str, days_back: int,
                       today: date) -> tuple[bool, date | None] | None:
    """Second-source earnings check via yfinance per-symbol dates.

    The calendar feed can simply lack a symbol (AXTI 2026-08-07), and a
    missing row reads as "didn't report" — the staleness family's exact
    shape. None means this source couldn't answer either; the caller must
    say so, never treat it as clear. Otherwise returns (reported within
    the window, most recent past report date): without the date, a bare
    "clear" on a move that news attributes to a report just outside the
    window reads as a calendar gap (BFLY/VRTX 2026-08-10).
    """
    import yfinance as yf

    try:
        df = yf.Ticker(symbol).get_earnings_dates(limit=8)
    except Exception:
        return None
    if df is None or len(df) == 0:
        return None
    past = [d.date() for d in df.index if d.date() <= today]
    window_start = today - timedelta(days=days_back)
    return any(d >= window_start for d in past), (max(past) if past else None)


def apply_earnings_cross_check(row: dict,
                               check: tuple[bool, date | None] | None,
                               today: date) -> None:
    """Fold the second-source answer into a candidate row: a confirmed
    recent report rejects it (PEAD turf), an unavailable source is labeled
    unverified — silence is how AXTI leaked. A clear verdict names the last
    known report so earnings follow-through outside the exclusion window is
    readable as such rather than as another calendar gap."""
    if check is None:
        row["earnings_check"] = ("unverified — second source unavailable; "
                                 "verify catalyst is not earnings via news")
        return
    within_window, last_report = check
    if within_window:
        row["rejects"].append(
            "recent_earnings (yfinance cross-check — missing from calendar feed)")
    elif last_report is not None:
        age = (today - last_report).days
        row["earnings_check"] = (
            f"clear — last reported {last_report.isoformat()} ({age}d ago); "
            "a move attributed to that report is follow-through, "
            "not a calendar gap")
    else:
        row["earnings_check"] = "clear — no past report on record"


def scan_movers(risk_config: dict, min_move_pct: float = 5.0,
                earnings_exclusion_days: int = 5,
                today: date | None = None,
                now: datetime | None = None) -> dict:
    today = today or date.today()
    floors = {
        "min_adv": float(risk_config["min_avg_dollar_volume"]),
        "min_price": float(risk_config.get("min_price", 5.0)),
        "min_move_pct": min_move_pct,
    }

    symbols = _screen_symbols()
    if not symbols:
        return {"scanned": 0, "passing": 0, "candidates": [],
                "rejected": [], "error": "screener returned no symbols"}

    # Names that reported earnings recently are PEAD's turf, not v2's.
    recent_earnings = {
        r.symbol for r in recent_reporters(earnings_exclusion_days, today=today)
    }

    history = fetch_history(symbols, period="3mo")
    candidates, rejected = [], []
    for sym in symbols:
        df = history.get(sym)
        rejects = []
        row = {"symbol": sym, "rejects": rejects}
        if sym in recent_earnings:
            rejects.append("recent_earnings (PEAD turf, not a news catalyst)")
        if df is None or len(df) < 21:
            rejects.append("insufficient price history")
        else:
            prior_close = float(df["Close"].iloc[-2])
            last = df.iloc[-1]
            move_pct = round(100 * (float(last["Close"]) / prior_close - 1), 2)
            pre = df.iloc[-21:-1]
            avg_vol = float(pre["Volume"].mean())
            adv = float((pre["Close"] * pre["Volume"]).mean())
            # The last bar is the live session when we scan intraday, so its
            # volume is only what has traded so far. Reported as a bare ratio
            # it reads as "no conviction" — 2026-08-20's entry window returned
            # 18 movers with every volume_ratio under 1.2x, which is not a
            # quiet tape, it is a partial numerator over a full denominator.
            complete = session_complete(
                df.index[-1].date() if hasattr(df.index[-1], "date")
                else df.index[-1], now)
            ratio = round(float(last["Volume"]) / avg_vol, 2) if avg_vol else 0.0
            basis = FULL_SESSION if complete else PARTIAL_SESSION
            row.update({
                "last_close": round(float(last["Close"]), 4),
                "move_pct": move_pct,
                "volume_ratio": ratio,
                "volume_basis": basis,
                "volume_verdict": volume_verdict(ratio, basis),
                "adv_dollar_20d": round(adv, 0),
            })
            if not complete:
                row["volume_note"] = (
                    f"volume_ratio is a FLOOR — session still trading. "
                    f"A floor at/above {V2_VOLUME_CONFIRM_RATIO}x is genuinely "
                    "confirmed (volume only accumulates); below it is "
                    "undetermined, not rejected — that call waits for the close"
                )
            if move_pct < floors["min_move_pct"]:
                rejects.append(f"move {move_pct}% < {floors['min_move_pct']}%")
            if adv < floors["min_adv"]:
                rejects.append(f"illiquid: ADV ${adv:,.0f}")
            if float(last["Close"]) < floors["min_price"]:
                rejects.append(f"price < ${floors['min_price']}")
        if not rejects:
            apply_earnings_cross_check(
                row, _reported_recently(sym, earnings_exclusion_days, today),
                today)
        (candidates if not rejects else rejected).append(row)

    candidates.sort(key=lambda c: c["move_pct"], reverse=True)
    return {
        "scanned": len(symbols),
        "passing": len(candidates),
        "candidates": candidates,
        "rejected": [{"symbol": r["symbol"], "rejects": r["rejects"]}
                     for r in rejected],
    }
