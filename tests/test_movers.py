"""Movers-scan earnings cross-check: the calendar feed lacking a symbol
must never read as "didn't report" (AXTI 2026-08-07, staleness family #4)."""

from autoswing.data.movers import apply_earnings_cross_check


def row():
    return {"symbol": "AXTI", "rejects": []}


class TestEarningsCrossCheck:
    def test_confirmed_recent_report_rejects(self):
        r = row()
        apply_earnings_cross_check(r, reported_recently=True)
        assert any("recent_earnings" in x for x in r["rejects"])

    def test_clean_check_passes_and_is_labeled(self):
        r = row()
        apply_earnings_cross_check(r, reported_recently=False)
        assert r["rejects"] == []
        assert r["earnings_check"] == "clear"

    def test_unavailable_source_is_unverified_not_clear(self):
        # Silence is the failure mode — an unanswerable check must be
        # visibly unverified, never silently equivalent to "clear".
        r = row()
        apply_earnings_cross_check(r, reported_recently=None)
        assert r["rejects"] == []
        assert "unverified" in r["earnings_check"]
