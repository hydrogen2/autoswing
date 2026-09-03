"""Movers-scan earnings cross-check: the calendar feed lacking a symbol
must never read as "didn't report" (AXTI 2026-08-07, staleness family #4),
and a bare "clear" must not hide a report just outside the exclusion
window (BFLY/VRTX 2026-08-10 — moves news-attributed to week-old reports
looked like calendar gaps).

Plus the partial-session volume basis (2026-08-20, same family: a broken
measurement rendering as a benign reading)."""

import json
from datetime import date, datetime, timezone

import pandas as pd

from autoswing.data import movers
from autoswing.data.movers import (
    apply_earnings_cross_check, resolve_pending_movers, scan_movers,
)
from autoswing.data.prices import (
    CONFIRMED, FULL_SESSION, PARTIAL_SESSION, UNCONFIRMED, UNDETERMINED,
    session_complete, volume_verdict,
)

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


class TestVolumeVerdict:
    """Floor semantics on a partial bar (2026-08-21).

    The preclose window fires at 15:30 ET, so the reaction bar is never
    complete when step 3d looks at it. "Cannot be judged until the close"
    therefore made the v2 volume gate a permanent no-op. A partial ratio is
    a strict floor, so the positive call is safe early; only the negative
    one has to wait.
    """

    def test_partial_floor_above_threshold_confirms(self):
        # MRNA 3.99x at 15:30 — it can only go up from here
        assert volume_verdict(3.99, PARTIAL_SESSION) == CONFIRMED
        assert volume_verdict(2.0, PARTIAL_SESSION) == CONFIRMED

    def test_partial_floor_below_threshold_is_undetermined_not_rejected(self):
        assert volume_verdict(1.15, PARTIAL_SESSION) == UNDETERMINED
        assert volume_verdict(1.99, PARTIAL_SESSION) == UNDETERMINED

    def test_complete_bar_below_threshold_is_a_real_rejection(self):
        assert volume_verdict(1.15, FULL_SESSION) == UNCONFIRMED

    def test_complete_bar_above_threshold_confirms(self):
        assert volume_verdict(2.4, FULL_SESSION) == CONFIRMED

    def test_partial_bar_never_yields_a_negative_call(self):
        for r in (0.0, 0.5, 1.0, 1.99):
            assert volume_verdict(r, PARTIAL_SESSION) != UNCONFIRMED

    def test_threshold_is_overridable(self):
        assert volume_verdict(1.5, PARTIAL_SESSION, threshold=1.2) == CONFIRMED
        assert volume_verdict(1.5, FULL_SESSION, threshold=3.0) == UNCONFIRMED


def mover_df(end: date, periods: int, last_volume: float,
             last_close: float = 109.5) -> pd.DataFrame:
    """Flat 100-close / 1M-volume history with a spike on the final bar."""
    idx = pd.bdate_range(end=end.isoformat(), periods=periods)
    close = [100.0] * (periods - 1) + [last_close]
    vol = [1_000_000.0] * (periods - 1) + [last_volume]
    return pd.DataFrame(
        {"Open": close, "High": close, "Low": close,
         "Close": close, "Volume": vol}, index=idx)


class TestResolvePendingMovers:
    """An undetermined partial-floor verdict must actually get its
    completed-bar re-check (CNH 2026-09-02: 'undetermined — re-check
    tomorrow' could never execute because the screener only surfaces
    today's movers, so the verdict expired silently)."""

    SESSION = date(2026, 9, 2)
    ENTRY = {"symbol": "CNH", "session": "2026-09-02",
             "move_pct": 9.5, "volume_ratio_floor": 1.86}

    def test_completed_bar_confirms(self):
        # 2.5M on the completed bar vs 1M avg — the floor resolved upward.
        hist = {"CNH": mover_df(self.SESSION, 22, 2_500_000)}
        resolved, pending = resolve_pending_movers(
            [self.ENTRY], hist, date(2026, 9, 3), et(2026, 9, 3, 10, 5))
        assert pending == []
        assert resolved[0]["volume_verdict"] == CONFIRMED
        assert resolved[0]["volume_ratio"] == 2.5
        assert resolved[0]["move_pct"] == 9.5  # completed close-to-close

    def test_completed_bar_below_threshold_is_the_deferred_negative(self):
        # The negative call the partial floor deferred finally lands.
        hist = {"CNH": mover_df(self.SESSION, 22, 1_900_000)}
        resolved, pending = resolve_pending_movers(
            [self.ENTRY], hist, date(2026, 9, 3), et(2026, 9, 3, 10, 5))
        assert pending == []
        assert resolved[0]["volume_verdict"] == UNCONFIRMED

    def test_same_session_rescan_stays_pending(self):
        # Preclose fires 15:30 ET; the bar is still trading — no new info.
        hist = {"CNH": mover_df(self.SESSION, 22, 1_860_000)}
        resolved, pending = resolve_pending_movers(
            [self.ENTRY], hist, self.SESSION, et(2026, 9, 2, 15, 30))
        assert resolved == []
        assert pending == [self.ENTRY]

    def test_fetch_gap_stays_pending_until_expiry(self):
        resolved, pending = resolve_pending_movers(
            [self.ENTRY], {}, date(2026, 9, 3), et(2026, 9, 3, 10, 5))
        assert resolved == []
        assert pending == [self.ENTRY]

    def test_stale_unresolvable_entry_expires_visibly(self):
        # Silence is the failure mode — an entry that can never resolve
        # must report as expired, not linger or vanish.
        resolved, pending = resolve_pending_movers(
            [self.ENTRY], {}, date(2026, 9, 14), et(2026, 9, 14, 10, 5))
        assert pending == []
        assert "expired" in resolved[0]["status"]


class TestScanMoversRecheckRoundTrip:
    """scan-movers persists undetermined floors and re-verdicts them on the
    next scan even when the symbol no longer appears in the screener."""

    def _patch(self, monkeypatch, screener, history):
        monkeypatch.setattr(movers, "_screen_symbols", lambda: screener)
        monkeypatch.setattr(movers, "fetch_history",
                            lambda syms, period="3mo": history)
        monkeypatch.setattr(movers, "recent_reporters",
                            lambda days, today: [])
        monkeypatch.setattr(movers, "_reported_recently",
                            lambda sym, days, today: (False, None))

    RISK = {"min_avg_dollar_volume": 5_000_000, "min_price": 5.0}

    def test_round_trip(self, monkeypatch, tmp_path):
        # Scan 1, midsession 09-02: CNH +9.5% on a 1.86x partial floor.
        self._patch(monkeypatch, ["CNH"],
                    {"CNH": mover_df(date(2026, 9, 2), 22, 1_860_000)})
        r1 = scan_movers(self.RISK, today=date(2026, 9, 2),
                         now=et(2026, 9, 2, 15, 30), state_dir=tmp_path)
        assert r1["candidates"][0]["volume_verdict"] == UNDETERMINED
        assert r1["pending_recheck"] == ["CNH 2026-09-02"]
        assert json.loads((tmp_path / "pending_movers.json").read_text())

        # Scan 2, next morning: CNH did NOT move again, so the screener no
        # longer surfaces it — the old code lost the verdict right here.
        df2 = mover_df(date(2026, 9, 3), 23, 1_000_000, last_close=110.0)
        df2.loc[df2.index[-2], "Volume"] = 2_500_000.0
        df2.loc[df2.index[-2], "Close"] = 109.5
        self._patch(monkeypatch, ["OTHR"],
                    {"CNH": df2,
                     "OTHR": mover_df(date(2026, 9, 3), 22, 1_000_000,
                                      last_close=100.5)})
        r2 = scan_movers(self.RISK, today=date(2026, 9, 3),
                         now=et(2026, 9, 3, 10, 5), state_dir=tmp_path)
        (recheck,) = r2["prior_session_recheck"]
        assert recheck["symbol"] == "CNH"
        assert recheck["volume_verdict"] == CONFIRMED
        assert r2["pending_recheck"] == []
        assert json.loads((tmp_path / "pending_movers.json").read_text()) == []
