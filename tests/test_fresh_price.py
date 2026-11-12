"""A current price beside the broker's delayed quote (2026-11-12).

IB quotes on the paper account are ~15 minutes old and nothing said so;
entry limits were priced off them (DELL 09-03 and CBRL 09-24 never filled
and ran to target). The fix adds a fresher source, and the thing these
tests guard hardest is that the fresher source can never fail QUIETLY: a
missing, old, or implausible price must change the label, not just the
number.
"""

import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from autoswing.commands.trading import _get_quote
from autoswing.data.fresh import (
    FRESH, FRESH_MAX_AGE_SECONDS, STALE, UNAVAILABLE, fresh_price,
    reference_quote,
)
from autoswing.journal import Journal

# Thursday 2026-11-12, 10:05 ET: inside the entry window.
IN_SESSION = datetime(2026, 11, 12, 15, 5, 0, tzinfo=timezone.utc)
# Same day, 00:00 ET: the market is closed.
OVERNIGHT = datetime(2026, 11, 12, 5, 0, 0, tzinfo=timezone.utc)


def bars(*rows, tz="America/New_York"):
    """rows: (utc datetime, close)."""
    idx = pd.DatetimeIndex([r[0] for r in rows], tz="UTC")
    idx = idx.tz_convert(tz) if tz else idx.tz_localize(None)
    return pd.DataFrame({"Close": [r[1] for r in rows]}, index=idx)


def fetcher(meta=None, frame=None):
    return lambda symbol: {"meta": meta or {}, "bars": frame}


def meta(price, when):
    return {"regularMarketPrice": price, "regularMarketTime": int(when.timestamp())}


class TestFreshPrice:
    def test_last_trade_seconds_old_is_fresh(self):
        f = fresh_price("XOM", now=IN_SESSION,
                        fetch=fetcher(meta(101.25, IN_SESSION - timedelta(seconds=20))))
        assert f["status"] == FRESH and f["price"] == 101.25
        assert f["basis"] == "last_trade" and f["age_seconds"] == 20
        assert f["as_of"] == "2026-11-12T15:04:40+00:00"
        assert f["reason"] is None and f["market_open"] is True

    def test_newest_dated_print_wins(self):
        # Chart metadata lags; the 1-minute bar is newer, so it is the price.
        f = fresh_price("XOM", now=IN_SESSION, fetch=fetcher(
            meta(100.0, IN_SESSION - timedelta(minutes=4)),
            bars((IN_SESSION - timedelta(minutes=2), 100.4),
                 (IN_SESSION - timedelta(seconds=30), 100.9))))
        assert f["basis"] == "1m_bar_close" and f["price"] == 100.9
        assert f["age_seconds"] == 30 and f["status"] == FRESH

    def test_old_print_in_session_is_stale_and_says_why(self):
        age = FRESH_MAX_AGE_SECONDS + 60
        f = fresh_price("XOM", now=IN_SESSION,
                        fetch=fetcher(meta(101.0, IN_SESSION - timedelta(seconds=age))))
        assert f["status"] == STALE and f["price"] == 101.0
        assert f["age_seconds"] == age
        assert "thin name or lagging feed" in f["reason"]

    def test_boundary_age_is_still_fresh(self):
        f = fresh_price("XOM", now=IN_SESSION, fetch=fetcher(
            meta(101.0, IN_SESSION - timedelta(seconds=FRESH_MAX_AGE_SECONDS))))
        assert f["status"] == FRESH

    def test_closed_market_is_stale_for_the_expected_reason(self):
        last_close = datetime(2026, 11, 11, 21, 0, 1, tzinfo=timezone.utc)
        f = fresh_price("XOM", now=OVERNIGHT, fetch=fetcher(meta(101.0, last_close)))
        assert f["status"] == STALE and f["market_open"] is False
        assert "market closed" in f["reason"]

    def test_fetch_error_is_unavailable_never_a_price(self):
        def boom(symbol):
            raise ConnectionError("yahoo down")
        f = fresh_price("XOM", now=IN_SESSION, fetch=boom)
        assert f["status"] == UNAVAILABLE and f["price"] is None
        assert f["age_seconds"] is None and "yahoo down" in f["reason"]

    def test_hung_fetch_is_cut_off_at_the_deadline(self):
        def hang(symbol):
            time.sleep(2)
            return {"meta": meta(1.0, IN_SESSION), "bars": None}
        started = time.monotonic()
        f = fresh_price("XOM", now=IN_SESSION, fetch=hang, deadline=0.05)
        assert time.monotonic() - started < 1.0
        assert f["status"] == UNAVAILABLE and "no answer" in f["reason"]

    def test_future_stamp_is_refused(self):
        # A timezone bug would make an old price look brand new.
        f = fresh_price("XOM", now=IN_SESSION,
                        fetch=fetcher(meta(101.0, IN_SESSION + timedelta(hours=5))))
        assert f["status"] == UNAVAILABLE and f["price"] is None
        assert "future" in f["reason"]

    def test_small_clock_skew_is_tolerated(self):
        f = fresh_price("XOM", now=IN_SESSION,
                        fetch=fetcher(meta(101.0, IN_SESSION + timedelta(seconds=30))))
        assert f["status"] == FRESH and f["age_seconds"] == 0

    def test_empty_response_is_unavailable(self):
        f = fresh_price("XOM", now=IN_SESSION, fetch=fetcher({}, bars()))
        assert f["status"] == UNAVAILABLE
        assert f["reason"] == "no dated price in the response"

    @pytest.mark.parametrize("bad", [float("nan"), 0, -5.0, None, "n/a"])
    def test_unusable_prices_are_ignored(self, bad):
        f = fresh_price("XOM", now=IN_SESSION, fetch=fetcher(
            {"regularMarketPrice": bad,
             "regularMarketTime": int(IN_SESSION.timestamp())}))
        assert f["status"] == UNAVAILABLE and f["price"] is None

    def test_bar_without_timezone_cannot_be_aged_so_is_not_used(self):
        f = fresh_price("XOM", now=IN_SESSION, fetch=fetcher(
            {}, bars((IN_SESSION - timedelta(seconds=30), 100.9), tz=None)))
        assert f["status"] == UNAVAILABLE

    def test_unreadable_payload_fails_soft(self):
        f = fresh_price("XOM", now=IN_SESSION,
                        fetch=lambda s: {"meta": {}, "bars": object()})
        assert f["status"] == UNAVAILABLE and "unreadable" in f["reason"]

    def test_default_fetch_is_offline_in_the_suite(self):
        # conftest disables the network fetch: no test reaches Yahoo by
        # accident, and the offline answer is the labelled fallback.
        f = fresh_price("XOM", now=IN_SESSION)
        assert f["status"] == UNAVAILABLE
        assert "network disabled in tests" in f["reason"]


def ib(last=100.0, mdt=3, **extra):
    q = {"symbol": "XOM", "bid": 99.9, "ask": 100.1, "last": last,
         "close": 99.5, "volume": 1000.0, "market_data_type": mdt}
    q.update(extra)
    return q


def fresh(price=100.6, status=FRESH, age=20, market_open=True, reason=None):
    return {"source": "yahoo", "basis": "last_trade", "price": price,
            "as_of": "2026-11-12T15:04:40+00:00", "age_seconds": age,
            "status": status, "market_open": market_open,
            "session": "regular session (closes 16:00 ET)" if market_open
                       else "pre-open (session 09:30-16:00 ET)",
            "reason": reason}


class TestReferenceQuote:
    def test_fresh_price_becomes_the_reference(self):
        r = reference_quote(ib(), fresh())
        assert r["reference_price"] == 100.6 and r["reference_basis"] == "fresh"
        assert r["fresh_vs_ib_last_pct"] == 0.6
        assert r["ib_quote_basis"] == "ib_delayed_15m"
        assert "DELAYED about 15 minutes" in r["ib_quote_note"]

    def test_broker_quote_keys_are_untouched(self):
        q = ib()
        r = reference_quote(q, fresh())
        assert {k: r[k] for k in q} == q

    def test_in_session_failure_falls_back_loudly(self):
        r = reference_quote(ib(), fresh(price=None, status=UNAVAILABLE, age=None,
                                        reason="ConnectionError: yahoo down"))
        assert r["reference_price"] == 100.0
        assert r["reference_basis"] == "ib_delayed_15m"
        assert "NO usable fresh price" in r["reference_note"]
        assert "yahoo down" in r["reference_note"]
        assert r["fresh_vs_ib_last_pct"] is None

    def test_in_session_stale_print_is_not_the_reference(self):
        r = reference_quote(ib(), fresh(status=STALE, age=700, reason="old"))
        assert r["reference_price"] == 100.0
        assert r["reference_basis"] == "ib_delayed_15m"
        assert r["fresh_vs_ib_last_pct"] == 0.6  # still shown, both labelled

    def test_closed_market_is_labelled_calmly(self):
        r = reference_quote(ib(), fresh(status=STALE, age=29000, market_open=False,
                                        reason="market closed"))
        assert r["reference_basis"] == "market_closed"
        assert r["reference_price"] == 100.0
        assert "NO usable" not in r["reference_note"]

    def test_implausible_gap_gives_no_reference_at_all(self):
        r = reference_quote(ib(last=100.0), fresh(price=115.0))
        assert r["reference_basis"] == "conflict" and r["reference_price"] is None
        assert "+15.00%" in r["reference_note"]

    def test_largest_measured_gap_is_accepted(self):
        # 3.77% was the widest of 142 entry-window quotes: real, not a glitch.
        r = reference_quote(ib(last=100.0), fresh(price=103.77))
        assert r["reference_basis"] == "fresh"

    def test_seat_blackout_still_gets_a_real_price(self):
        # 10197: the owner's phone holds the data seat and IB returns nulls.
        q = ib(last=None, mdt=1, bid=None, ask=None, close=None,
               error="10197 competing live session")
        r = reference_quote(q, fresh())
        assert r["ib_quote_basis"] == "ib_empty"
        assert r["reference_price"] == 100.6 and r["reference_basis"] == "fresh"
        assert r["fresh_vs_ib_last_pct"] is None and "ib_quote_note" not in r

    def test_empty_type_1_quote_is_not_mistaken_for_real_time(self):
        # ib_async leaves marketDataType at 1 when no tick ever arrives.
        r = reference_quote(ib(last=None, mdt=1), fresh(price=None, status=UNAVAILABLE,
                                                        age=None, reason="x"))
        assert r["ib_quote_basis"] == "ib_empty"
        assert r["reference_basis"] == "none" and r["reference_price"] is None

    def test_ib_no_data_sentinel_is_an_empty_quote(self):
        # Seen live off-hours 2026-11-12: bid/ask/last all -1, close real.
        r = reference_quote(ib(last=-1.0, bid=-1, ask=-1), fresh())
        assert r["ib_quote_basis"] == "ib_empty"
        assert r["fresh_vs_ib_last_pct"] is None
        assert r["reference_price"] == 100.6 and r["reference_basis"] == "fresh"

    def test_real_time_ib_quote_outranks_yahoo(self):
        r = reference_quote(ib(last=100.0, mdt=1), fresh())
        assert r["reference_basis"] == "ib_realtime" and r["reference_price"] == 100.0
        assert "ib_quote_note" not in r

    def test_unknown_data_type_is_assumed_delayed(self):
        r = reference_quote(ib(mdt=None), fresh(price=None, status=UNAVAILABLE,
                                                age=None, reason="x"))
        assert r["ib_quote_basis"] == "ib_delayed_15m"


class QuoteBroker:
    def __init__(self, tmp_path, quote):
        self.journal = Journal(tmp_path)
        self._quote = quote

    def get_quote(self, symbol):
        return dict(self._quote)


def journal_events(tmp_path, name):
    rows = []
    for f in sorted(tmp_path.glob("*.jsonl")):
        rows += [json.loads(line) for line in f.read_text().splitlines()]
    return [r for r in rows if r["event"] == name]


class TestGetQuoteCommand:
    def test_adds_the_fresh_block_and_journals_it(self, tmp_path, monkeypatch):
        monkeypatch.setattr("autoswing.data.fresh.fresh_price", lambda s: fresh())
        r = _get_quote(QuoteBroker(tmp_path, ib()), "xom")
        assert r["last"] == 100.0 and r["close"] == 99.5   # health check reads these
        assert r["reference_price"] == 100.6 and r["fresh"]["age_seconds"] == 20
        (ev,) = journal_events(tmp_path, "quote.fresh")
        assert ev["symbol"] == "XOM" and ev["ib_last"] == 100.0
        assert ev["reference_basis"] == "fresh" and ev["fresh_vs_ib_last_pct"] == 0.6
        assert ev["fresh"]["price"] == 100.6

    def test_yahoo_failure_never_breaks_the_quote(self, tmp_path):
        # Offline by conftest: the IB quote still comes back whole, and the
        # fallback is labelled rather than silent.
        r = _get_quote(QuoteBroker(tmp_path, ib()), "XOM")
        assert r["last"] == 100.0 and r["fresh"]["status"] == UNAVAILABLE
        assert r["reference_basis"] in ("ib_delayed_15m", "market_closed")
        (ev,) = journal_events(tmp_path, "quote.fresh")
        assert ev["fresh"]["status"] == UNAVAILABLE

    def test_blackout_shape_is_unchanged_for_the_health_check(self, tmp_path):
        q = ib(last=None, close=None, bid=None, ask=None, mdt=1, error="10197")
        r = _get_quote(QuoteBroker(tmp_path, q), "XOM")
        assert not (r.get("close") or r.get("last"))   # still reads as blackout
        assert r["error"] == "10197"
