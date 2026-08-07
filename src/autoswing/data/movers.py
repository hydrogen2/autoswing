"""Movers scan for the v2 news-catalyst shadow strategy.

Surfaces big price+volume movers that are NOT explained by a recent
earnings report — those belong to PEAD. The brain identifies the actual
catalyst (FDA, M&A fallout, guidance, contract, upgrade) via news search;
this module only finds "something happened here" candidates.
"""

from __future__ import annotations

from datetime import date, timedelta

from .earnings import recent_reporters
from .prices import fetch_history


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
                       today: date) -> bool | None:
    """Second-source earnings check via yfinance per-symbol dates.

    The calendar feed can simply lack a symbol (AXTI 2026-08-07), and a
    missing row reads as "didn't report" — the staleness family's exact
    shape. None means this source couldn't answer either; the caller must
    say so, never treat it as clear.
    """
    import yfinance as yf

    try:
        df = yf.Ticker(symbol).get_earnings_dates(limit=8)
    except Exception:
        return None
    if df is None or len(df) == 0:
        return None
    window_start = today - timedelta(days=days_back)
    return any(window_start <= d.date() <= today for d in df.index)


def apply_earnings_cross_check(row: dict,
                               reported_recently: bool | None) -> None:
    """Fold the second-source answer into a candidate row: a confirmed
    recent report rejects it (PEAD turf), an unavailable source is labeled
    unverified — silence is how AXTI leaked."""
    if reported_recently is True:
        row["rejects"].append(
            "recent_earnings (yfinance cross-check — missing from calendar feed)")
    elif reported_recently is None:
        row["earnings_check"] = ("unverified — second source unavailable; "
                                 "verify catalyst is not earnings via news")
    else:
        row["earnings_check"] = "clear"


def scan_movers(risk_config: dict, min_move_pct: float = 5.0,
                earnings_exclusion_days: int = 5,
                today: date | None = None) -> dict:
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
            row.update({
                "last_close": round(float(last["Close"]), 4),
                "move_pct": move_pct,
                "volume_ratio": round(float(last["Volume"]) / avg_vol, 2)
                if avg_vol else 0.0,
                "adv_dollar_20d": round(adv, 0),
            })
            if move_pct < floors["min_move_pct"]:
                rejects.append(f"move {move_pct}% < {floors['min_move_pct']}%")
            if adv < floors["min_adv"]:
                rejects.append(f"illiquid: ADV ${adv:,.0f}")
            if float(last["Close"]) < floors["min_price"]:
                rejects.append(f"price < ${floors['min_price']}")
        if not rejects:
            apply_earnings_cross_check(
                row, _reported_recently(sym, earnings_exclusion_days, today))
        (candidates if not rejects else rejected).append(row)

    candidates.sort(key=lambda c: c["move_pct"], reverse=True)
    return {
        "scanned": len(symbols),
        "passing": len(candidates),
        "candidates": candidates,
        "rejected": [{"symbol": r["symbol"], "rejects": r["rejects"]}
                     for r in rejected],
    }
