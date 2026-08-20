"""Movers-scan earnings cross-check: the calendar feed lacking a symbol
must never read as "didn't report" (AXTI 2026-08-07, staleness family #4),
and a bare "clear" must not hide a report just outside the exclusion
window (BFLY/VRTX 2026-08-10 — moves news-attributed to week-old reports
looked like calendar gaps).

Plus the partial-session volume basis (2026-08-20, same family: a broken
measurement rendering as a benign reading)."""

from datetime import date, datetime, timezone

from autoswing.data.movers import apply_earnings_cross_check
from autoswing.data.prices import session_complete

TODAY = date(2026, 8, 10)


def et(y, m, d, hh, mm=0):
    """A UTC instant; the helpers convert to ET themselves."""
    from zoneinfo import ZoneInfo
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("America/New_York"))


class TestSessionComplete:
    def test_past_session_is_complete(self):
        assert session_complete(date(2026, 8, 19), et(2026, 8, 20, 10, 5))

    def test_todays_bar_midsession_is_incomplete(self):
        # 2026-08-20 entry window: 10:05 ET. Today's bar has ~5.5 hours of
        # volume left to accumulate, so its ratio is not yet a measurement.
        assert not session_complete(date(2026, 8, 20), et(2026, 8, 20, 10, 5))

    def test_todays_bar_after_close_is_complete(self):
        assert session_complete(date(2026, 8, 20), et(2026, 8, 20, 16, 30))

    def test_premarket_treats_todays_bar_as_incomplete(self):
        # The premarket window runs 08:00 ET — before the open, so any bar
        # dated today holds no volume at all.
        assert not session_complete(date(2026, 8, 20), et(2026, 8, 20, 8, 0))

    def test_future_bar_is_not_complete(self):
        assert not session_complete(date(2026, 8, 21), et(2026, 8, 20, 16, 30))

    def test_utc_instant_is_converted_to_et(self):
        # 20:00 UTC is 16:00 ET (closed); 19:00 UTC is 15:00 ET (open).
        # Comparing the raw UTC hour against 16 would invert both.
        day = date(2026, 8, 20)
        assert session_complete(day, datetime(2026, 8, 20, 20, 0, tzinfo=timezone.utc))
        assert not session_complete(day, datetime(2026, 8, 20, 19, 0, tzinfo=timezone.utc))


def row():
    return {"symbol": "AXTI", "rejects": []}


class TestEarningsCrossCheck:
    def test_confirmed_recent_report_rejects(self):
        r = row()
        apply_earnings_cross_check(r, (True, date(2026, 8, 7)), TODAY)
        assert any("recent_earnings" in x for x in r["rejects"])

    def test_clean_check_passes_and_is_labeled(self):
        r = row()
        apply_earnings_cross_check(r, (False, None), TODAY)
        assert r["rejects"] == []
        assert r["earnings_check"].startswith("clear")

    def test_unavailable_source_is_unverified_not_clear(self):
        # Silence is the failure mode — an unanswerable check must be
        # visibly unverified, never silently equivalent to "clear".
        r = row()
        apply_earnings_cross_check(r, None, TODAY)
        assert r["rejects"] == []
        assert "unverified" in r["earnings_check"]

    def test_clear_names_report_outside_window(self):
        # BFLY reported 2026-07-30, VRTX 2026-08-03; both moved big on
        # 2026-08-10 with news attributing the move to those reports.
        # Outside the 5-day window that's a legitimate "clear", but the
        # verdict must carry the report date so the brain reads it as
        # earnings follow-through, not a second calendar gap.
        r = row()
        apply_earnings_cross_check(r, (False, date(2026, 8, 3)), TODAY)
        assert r["rejects"] == []
        assert r["earnings_check"].startswith("clear")
        assert "2026-08-03" in r["earnings_check"]
        assert "7d ago" in r["earnings_check"]
