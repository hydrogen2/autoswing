"""Option-chain fetch for the wheel book (yfinance, free, no key).

Only this module touches the network; everything the screen decides with
lives in wheel.py and is unit-testable on synthetic quotes.

A note on what this data can and cannot support: yfinance serves the CURRENT
chain only, with no history. That rules out backtesting the wheel, and the
obvious workaround is a trap — estimating past premiums from realized vol
via Black-Scholes would set the implied-minus-realized gap to zero by
construction, which is the exact quantity the strategy lives on. So this
book can only ever be measured FORWARD, on quotes captured at decision time.
"""

from __future__ import annotations

import datetime as dt

from ..wheel import (
    MAX_SPREAD_PCT, MIN_OPEN_INTEREST, PutQuote, TARGET_OTM, realized_vol,
)


def _atm_iv(puts, spot: float) -> float | None:
    p = puts[(puts.bid > 0) & (puts.openInterest > 0)]
    if p.empty:
        return None
    row = p.iloc[(p.strike - spot).abs().argsort()[:1]]
    return float(row.impliedVolatility.iloc[0])


def front_slope(ticker, spot: float, today: dt.date) -> float | None:
    """Front-month ATM IV minus the ~85-day ATM IV.

    A positive slope means the near contract is richer than the far one. With
    earnings already excluded, that inversion says the market is pricing a
    specific near-term event we have not identified — and selling a put into
    an event you cannot name is the failure mode this screen exists to avoid.
    """
    front = back = None
    for e in (ticker.options or [])[:10]:
        try:
            d = (dt.date.fromisoformat(e) - today).days
        except ValueError:
            continue
        if not (5 <= d <= 200):
            continue
        try:
            iv = _atm_iv(ticker.option_chain(e).puts, spot)
        except Exception:
            continue
        if iv is None:
            continue
        if 15 <= d <= 35 and front is None:
            front = iv
        if d >= 70 and back is None:
            back = iv
    if front is None or back is None:
        return None
    return round(front - back, 4)


def next_earnings(ticker, today: dt.date) -> str | None:
    try:
        ed = ticker.get_earnings_dates(limit=8)
    except Exception:
        return None
    if ed is None or not len(ed):
        return None
    future = [i.date() for i in ed.index if i.date() >= today]
    return min(future).isoformat() if future else None


def fetch_put_quotes(symbols, collateral_cap: float, today: dt.date | None = None,
                     dte_min: int = 20, dte_max: int = 60,
                     target_otm: float = TARGET_OTM) -> tuple[list[PutQuote], list[dict]]:
    """One candidate put per symbol: the strike nearest target_otm below spot
    whose collateral fits the cap. Returns (quotes, skipped) — a symbol that
    yields nothing is REPORTED, never silently dropped, because an empty
    screen and a broken fetch look identical otherwise."""
    import yfinance as yf

    today = today or dt.date.today()
    quotes: list[PutQuote] = []
    skipped: list[dict] = []

    for sym in symbols:
        sym = sym.upper().strip()
        if not sym:
            continue
        try:
            tk = yf.Ticker(sym)
            hist = tk.history(period="2y", auto_adjust=False)["Close"].dropna()
            if len(hist) < 130:
                skipped.append({"symbol": sym, "why": "insufficient_history"})
                continue
            closes = [float(x) for x in hist]
            spot = closes[-1]

            expiries = []
            for e in (tk.options or []):
                try:
                    d = (dt.date.fromisoformat(e) - today).days
                except ValueError:
                    continue
                if dte_min <= d <= dte_max:
                    expiries.append((d, e))
            if not expiries:
                skipped.append({"symbol": sym, "why": "no_expiry_in_dte_window"})
                continue
            dte, expiry = sorted(expiries)[0]

            puts = tk.option_chain(expiry).puts
            if puts is None or puts.empty:
                skipped.append({"symbol": sym, "why": "empty_chain"})
                continue
            c = puts[(puts.bid > 0) & (puts.strike < spot)
                     & (puts.strike * 100 <= collateral_cap)].copy()
            if c.empty:
                skipped.append({"symbol": sym, "why": "no_strike_under_collateral_cap"})
                continue
            c["d"] = (c.strike / spot - 1 + target_otm).abs()
            # Liquidity first, THEN nearest to target. Picking the closest
            # strike to 8% OTM and only afterwards noticing it is untradeable
            # rejects the NAME for a property of a contract we chose badly --
            # a neighbouring strike is often both liquid and near target.
            c["_mid"] = (c.bid + c.ask) / 2
            c["_spr"] = (c.ask - c.bid) / c._mid.where(c._mid > 0)
            liquid = c[(c.openInterest.fillna(0) >= MIN_OPEN_INTEREST)
                       & (c._spr <= MAX_SPREAD_PCT)]
            # Falling back to the illiquid best keeps the name on the board
            # with its REAL rejection reason, rather than vanishing from the
            # report as if it had never been looked at.
            pool, liquid_strike = (liquid, True) if not liquid.empty else (c, False)
            row = pool.sort_values("d").iloc[0]

            quotes.append(PutQuote(
                symbol=sym, spot=round(spot, 4), strike=float(row.strike),
                bid=float(row.bid), ask=float(row.ask), expiry=expiry, dte=dte,
                iv=round(float(row.impliedVolatility), 4),
                open_interest=int(row.openInterest or 0),
                rv120=realized_vol(closes, 120), rv252=realized_vol(closes, 252),
                front_slope=front_slope(tk, spot, today),
                earnings_date=next_earnings(tk, today),
                note="" if liquid_strike else "no strike met the liquidity bar",
            ))
        except Exception as ex:
            skipped.append({"symbol": sym, "why": f"fetch_error: {type(ex).__name__}"})
    return quotes, skipped
