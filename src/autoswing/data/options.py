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
    # Choose the two expiry DATES first, then fetch exactly two chains.
    # Walking every expiry and fetching as it went cost ~10 network calls per
    # symbol and blew the CLI watchdog on a full universe.
    dated = []
    for e in (ticker.options or []):
        try:
            dated.append(((dt.date.fromisoformat(e) - today).days, e))
        except ValueError:
            continue
    near = next((e for d, e in sorted(dated) if 15 <= d <= 35), None)
    far = next((e for d, e in sorted(dated) if d >= 70), None)
    if near is None or far is None:
        return None
    try:
        front = _atm_iv(ticker.option_chain(near).puts, spot)
        back = _atm_iv(ticker.option_chain(far).puts, spot)
    except Exception:
        return None
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


def option_mark(symbol: str, expiry: str, strike: float,
                kind: str = "put") -> float | None:
    """Cost to buy back one open contract, per share. Uses the ASK, because
    closing a short means paying the offer -- marking a liability at the bid
    would flatter every open cycle."""
    import yfinance as yf

    try:
        chain = yf.Ticker(symbol).option_chain(expiry)
        df = chain.puts if kind == "put" else chain.calls
        row = df[df.strike == strike]
        if row.empty:
            return None
        ask = float(row.ask.iloc[0])
        bid = float(row.bid.iloc[0])
        # A zero ask is a missing quote, not a free unwind.
        if ask > 0:
            return round(ask, 4)
        return round(bid, 4) if bid > 0 else None
    except Exception:
        return None


def call_rows(symbol: str, today: dt.date, dte_min: int = 21,
              dte_max: int = 60) -> tuple[str | None, list[dict]]:
    """Call chain for the first expiry in the window, as plain dicts so the
    strike choice stays a pure, testable decision."""
    import yfinance as yf

    try:
        tk = yf.Ticker(symbol)
        cands = []
        for e in (tk.options or []):
            try:
                d = (dt.date.fromisoformat(e) - today).days
            except ValueError:
                continue
            if dte_min <= d <= dte_max:
                cands.append((d, e))
        if not cands:
            return None, []
        _, expiry = sorted(cands)[0]
        df = tk.option_chain(expiry).calls
        rows = [{"strike": float(r.strike), "bid": float(r.bid),
                 "ask": float(r.ask), "open_interest": int(r.openInterest or 0)}
                for r in df.itertuples()]
        return expiry, rows
    except Exception:
        return None, []
