"""Movers-scan earnings cross-check: the calendar feed lacking a symbol
must never read as "didn't report" (AXTI 2026-08-07, staleness family #4),
and a bare "clear" must not hide a report just outside the exclusion
window (BFLY/VRTX 2026-08-10 — moves news-attributed to week-old reports
looked like calendar gaps)."""

from datetime import date

from autoswing.data.movers import apply_earnings_cross_check

TODAY = date(2026, 8, 10)


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
