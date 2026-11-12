"""A current price to set beside the broker's delayed quote.

The paper account's IB quotes are market_data_type 3: delayed about 15
minutes. Measured 2026-11-12 on 426 journaled quotes: 97% sat inside the
Yahoo 5-minute bar from 15 minutes EARLIER, and at the entry window the
median gap to the real price was 0.58% (p90 1.83%) -- wider than the ~0.5%
no-chase allowance the entry limit is built on. DELL 09-03 and CBRL 09-24
were priced off that quote, never filled, and ran to target without us.

This module fetches Yahoo's last regular-session print with ITS OWN
timestamp. Every result carries an age and a status, and nothing here is
ever silently substituted for the broker quote: a failed or old fetch says
so ("unavailable" / "stale") and the caller falls back to the delayed
quote, labelled as delayed. A fallback that looked like a fresh price
would be the renders-as-benign failure this is meant to remove.

Only _yahoo_fetch() touches the network.
"""

from __future__ import annotations

import math
import threading
from datetime import datetime, timezone

from .prices import ET

# A print this old during the session is no longer "the real price": the
# name is thin or the feed is lagging, and either way the label must change.
FRESH_MAX_AGE_SECONDS = 300
# Clock skew allowance. A timestamp further ahead than this is a timezone
# or data bug, and a price we cannot age is a price we do not use.
FUTURE_TOLERANCE_SECONDS = 90
# Largest fresh-vs-delayed gap accepted without a human reason. The widest
# gap in 142 measured entry-window quotes was 3.77%; beyond 10% in a
# quarter-hour the likelier story is a wrong symbol, a halt, or a bad tick.
MAX_PLAUSIBLE_GAP_PCT = 10.0
FETCH_TIMEOUT_SECONDS = 6.0     # per HTTP request, passed to yfinance
FETCH_DEADLINE_SECONDS = 10.0   # hard wall-clock cap on the whole fetch

FRESH = "fresh"
STALE = "stale"
UNAVAILABLE = "unavailable"

IB_DELAYED = "ib_delayed_15m"
IB_REALTIME = "ib_realtime"
IB_EMPTY = "ib_empty"

IB_DELAYED_NOTE = (
    "IB data on this account is DELAYED about 15 minutes "
    "(market_data_type 3): bid/ask/last are where the stock WAS a "
    "quarter-hour ago, not where it is now")


def _yahoo_fetch(symbol: str, timeout: float) -> dict:
    """One chart request: today's 1-minute regular-session bars plus the
    chart metadata (last regular-market price and its epoch time)."""
    import logging

    import yfinance as yf

    # yfinance reports a failed download on stderr; callers parse our JSON
    # from a merged stream (scripts/healthcheck.sh), so keep it quiet. The
    # failure is still reported, as status "unavailable".
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    ticker = yf.Ticker(symbol.replace(".", "-"))
    bars = ticker.history(period="1d", interval="1m", prepost=False,
                          auto_adjust=False, timeout=timeout)
    return {"bars": bars, "meta": dict(ticker.history_metadata or {})}


def positive_price(value) -> float | None:
    """A finite price above zero, else None. IB sends -1 for "no data"."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x > 0 else None


def _prints(raw: dict) -> list[tuple[datetime, float, str]]:
    """Every dated price in a fetch result: (time UTC, price, basis)."""
    out = []
    meta = raw.get("meta") or {}
    price = positive_price(meta.get("regularMarketPrice"))
    stamp = positive_price(meta.get("regularMarketTime"))
    if price is not None and stamp is not None:
        out.append((datetime.fromtimestamp(stamp, timezone.utc), price,
                    "last_trade"))
    bars = raw.get("bars")
    if bars is not None and len(bars) and "Close" in bars:
        closes = bars["Close"].dropna()
        if len(closes):
            ts = closes.index[-1]
            price = positive_price(closes.iloc[-1])
            # A bar without a timezone cannot be aged, so it is not used.
            if price is not None and getattr(ts, "tzinfo", None) is not None:
                out.append((ts.to_pydatetime().astimezone(timezone.utc),
                            price, "1m_bar_close"))
    return out


def _with_deadline(fn, deadline: float):
    """(value, error). A hung fetch must not hang an entry window: the
    worker is a daemon thread and is simply abandoned at the deadline."""
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001 -- fail soft, report the cause
            box["error"] = f"{type(exc).__name__}: {exc}"[:300]

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(deadline)
    if worker.is_alive():
        return None, f"no answer from Yahoo within {deadline:.0f}s"
    return box.get("value"), box.get("error")


def _session(now: datetime) -> tuple[bool | None, str]:
    try:
        from ..calendar import is_market_open
        return is_market_open(now.astimezone(ET))
    except Exception as exc:  # noqa: BLE001 -- e.g. calendar past coverage
        return None, f"session unknown ({type(exc).__name__})"


def fresh_price(symbol: str, *, now: datetime | None = None, fetch=None,
                max_age_seconds: int = FRESH_MAX_AGE_SECONDS,
                deadline: float = FETCH_DEADLINE_SECONDS) -> dict:
    """Yahoo's latest regular-session print for `symbol`, with its age.

    status "fresh"       price is at most max_age_seconds old
           "stale"       a price came back but it is older than that
           "unavailable" no usable price; price is None
    `reason` is None when fresh and says why otherwise. Never raises.
    `fetch` and `now` are injectable for tests.
    """
    now = now or datetime.now(timezone.utc)
    market_open, session = _session(now)
    out = {
        "source": "yahoo", "basis": None, "price": None, "as_of": None,
        "age_seconds": None, "status": UNAVAILABLE,
        "market_open": market_open, "session": session, "reason": None,
    }
    fetch = fetch or (lambda s: _yahoo_fetch(s, FETCH_TIMEOUT_SECONDS))
    raw, error = _with_deadline(lambda: fetch(symbol), deadline)
    if error or raw is None:
        out["reason"] = error or "empty response"
        return out
    try:
        prints = _prints(raw)
    except Exception as exc:  # noqa: BLE001 -- unexpected payload shape
        out["reason"] = f"unreadable response: {type(exc).__name__}: {exc}"[:300]
        return out
    if not prints:
        out["reason"] = "no dated price in the response"
        return out
    as_of, price, basis = max(prints, key=lambda p: p[0])
    age = (now - as_of).total_seconds()
    if age < -FUTURE_TOLERANCE_SECONDS:
        out["reason"] = (f"price is stamped {-age:.0f}s in the future "
                        f"({as_of.isoformat()}): not trusted")
        return out
    out.update(basis=basis, price=round(price, 4),
               as_of=as_of.isoformat(timespec="seconds"),
               age_seconds=max(0, int(age)))
    if age <= max_age_seconds:
        out["status"] = FRESH
        return out
    out["status"] = STALE
    if market_open is False:
        out["reason"] = (f"market closed ({session}): this is the last "
                         f"regular-session print, not a live price")
    else:
        out["reason"] = (f"latest print is {int(age)}s old (limit "
                         f"{max_age_seconds}s): thin name or lagging feed")
    return out


def reference_quote(quote: dict, fresh: dict) -> dict:
    """The broker quote plus the fresh price, each labelled with its age,
    and ONE reference price to build a limit from.

    Additive: every key of the broker quote is returned unchanged, so
    `last`/`close` readers (the health check) see what they always saw.

    reference_basis "fresh"            Yahoo print, at most 5 minutes old
                    "ib_realtime"      IB itself is live (types 1/2)
                    "ib_delayed_15m"   IN SESSION with no usable fresh
                                       price: IB last, about 15 minutes old
                    "market_closed"    no live price exists; last prints
                    "conflict"         the two disagree implausibly: none
                    "none"             neither source has a price
    """
    out = dict(quote)
    ib_last = positive_price(quote.get("last"))
    if ib_last is None:
        ib_basis = IB_EMPTY
    elif quote.get("market_data_type") in (1, 2):
        ib_basis = IB_REALTIME
    else:
        ib_basis = IB_DELAYED  # 3/4, and anything unrecognised: assume old

    fresh_px = positive_price(fresh.get("price"))
    gap = None
    if fresh_px is not None and ib_last is not None:
        gap = round(100.0 * (fresh_px / ib_last - 1.0), 2)
    usable = fresh.get("status") == FRESH and fresh_px is not None

    if ib_basis == IB_REALTIME:
        ref, basis, note = ib_last, IB_REALTIME, "IB quote is real-time"
    elif usable and gap is not None and abs(gap) > MAX_PLAUSIBLE_GAP_PCT:
        ref, basis = None, "conflict"
        note = (f"fresh price {fresh_px} is {gap:+.2f}% from the delayed IB "
                f"last {ib_last}: too far for 15 minutes. Wrong symbol, "
                f"halt or news? Do not price a limit until you know which")
    elif usable:
        ref, basis = fresh_px, FRESH
        note = (f"Yahoo {fresh.get('basis')}, {fresh.get('age_seconds')}s "
                f"old. Price the limit from this, not from IB last")
    elif fresh.get("market_open") is False and (ib_last or fresh_px):
        # Expected outside the session, so it is labelled calmly: a loud
        # fallback note every pre-market would teach readers to skip it.
        ref, basis = ib_last or fresh_px, "market_closed"
        note = (f"market closed ({fresh.get('session')}): no live price "
                f"exists, these are last-session prints")
    elif ib_last is not None:
        ref, basis = ib_last, IB_DELAYED
        note = (f"NO usable fresh price ({fresh.get('status')}: "
                f"{fresh.get('reason')}). Falling back to IB last, which is "
                f"about 15 minutes old: say so, and expect the real price "
                f"to differ")
    else:
        ref, basis = None, "none"
        note = (f"no price from either source (fresh "
                f"{fresh.get('status')}: {fresh.get('reason')}; IB quote "
                f"empty)")

    out["ib_quote_basis"] = ib_basis
    if ib_basis == IB_DELAYED:
        out["ib_quote_note"] = IB_DELAYED_NOTE
    out["fresh"] = fresh
    out["fresh_vs_ib_last_pct"] = gap
    out["reference_price"] = ref
    out["reference_basis"] = basis
    out["reference_note"] = note
    return out
