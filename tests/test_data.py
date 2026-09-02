"""Tests for data-layer logic: calendar parsing, reaction math, floors.
All synthetic — no network."""

from datetime import date, timedelta

import pandas as pd
import pytest

from autoswing.data.candidates import build_candidate
from autoswing.data.earnings import Report, _money, parse_calendar_rows
from autoswing.data.prices import reaction_metrics


class TestMoneyParsing:
    def test_dollar(self):
        assert _money("$0.71") == 0.71

    def test_negative_parens(self):
        assert _money("($0.30)") == -0.30

    def test_negative_minus_sign(self):
        # Nasdaq also emits bare-minus negatives (ECOR 2023-08-09 '-$1.00');
        # losing the sign flips a loss estimate into a positive denominator.
        assert _money("-$1.00") == -1.00
        assert _money(" -$0.42") == -0.42

    def test_thousands(self):
        assert _money("$3,182,376,227") == 3182376227.0

    def test_empty(self):
        assert _money("") is None
        assert _money(None) is None


class TestCalendarParsing:
    def test_nasdaq_row(self):
        rows = [{
            "eps": "$0.71", "surprise": "44.9", "time": "time-after-hours",
            "symbol": "PENG", "name": "Penguin Solutions, Inc.",
            "marketCap": "$3,182,376,227", "epsForecast": "$0.49", "noOfEsts": "1",
        }]
        r = parse_calendar_rows(rows, date(2026, 7, 7))[0]
        assert r.symbol == "PENG"
        assert r.timing == "amc"
        assert r.eps_actual == 0.71
        assert r.surprise_pct == 44.9
        assert r.report_date == "2026-07-07"

    def test_missing_fields_tolerated(self):
        rows = [{"symbol": "XYZ", "time": "weird-new-value"}]
        r = parse_calendar_rows(rows, date(2026, 7, 7))[0]
        assert r.timing == "unknown"
        assert r.eps_actual is None
        assert r.surprise_pct is None

    def test_na_strings_tolerated(self):
        # Regression: live feed sent noOfEsts='N/A' and crashed the scan
        # (2026-07-09, bot correctly stood down and flagged it).
        rows = [{
            "symbol": "XYZ", "time": "time-after-hours",
            "noOfEsts": "N/A", "surprise": "N/A", "eps": "N/A",
            "epsForecast": "N/A", "marketCap": "N/A",
        }]
        r = parse_calendar_rows(rows, date(2026, 7, 7))[0]
        assert r.num_estimates is None
        assert r.surprise_pct is None
        assert r.market_cap is None

    def test_blank_symbol_dropped(self):
        assert parse_calendar_rows([{"symbol": " "}], date(2026, 7, 7)) == []


def make_df(closes, opens=None, volumes=None, start="2026-06-01"):
    idx = pd.bdate_range(start, periods=len(closes))
    return pd.DataFrame({
        "Open": opens or closes,
        "Close": closes,
        "Volume": volumes or [1_000_000] * len(closes),
    }, index=idx)


class TestReactionMetrics:
    def test_bmo_reaction_same_day(self):
        # 25 flat days at 100, then report day closes at 110.
        closes = [100.0] * 25 + [110.0]
        opens = [100.0] * 25 + [108.0]
        volumes = [1_000_000] * 25 + [5_000_000]
        df = make_df(closes, opens, volumes)
        report_day = df.index[25].date()
        r = reaction_metrics("T", df, report_day, "bmo")
        assert r.move_pct == 10.0
        assert r.gap_pct == 8.0
        assert r.volume_ratio == 5.0
        assert r.days_since_reaction == 0
        assert r.adv_dollar_20d == 100 * 1_000_000

    def test_reaction_bar_still_trading_is_flagged_partial(self):
        # 2026-08-20: a day-0 BMO reaction measured during the session divides
        # partial volume by a full-day average. The number is a floor, and it
        # must say so — an understated ratio is indistinguishable from a
        # genuine no-conviction move.
        from datetime import datetime
        from zoneinfo import ZoneInfo

        closes = [100.0] * 25 + [110.0]
        volumes = [1_000_000] * 25 + [900_000]  # partial: reads as 0.9x
        df = make_df(closes, None, volumes)
        report_day = df.index[25].date()
        midsession = datetime(report_day.year, report_day.month, report_day.day,
                              10, 5, tzinfo=ZoneInfo("America/New_York"))
        r = reaction_metrics("T", df, report_day, "bmo", now=midsession)
        assert r.volume_ratio == 0.9
        assert r.volume_basis == "partial_session"

    def test_completed_reaction_bar_is_full_session(self):
        closes = [100.0] * 25 + [110.0]
        volumes = [1_000_000] * 25 + [5_000_000]
        df = make_df(closes, None, volumes)
        report_day = df.index[25].date()
        from datetime import datetime
        from zoneinfo import ZoneInfo

        after_close = datetime(report_day.year, report_day.month, report_day.day,
                               16, 30, tzinfo=ZoneInfo("America/New_York"))
        r = reaction_metrics("T", df, report_day, "bmo", now=after_close)
        assert r.volume_ratio == 5.0
        assert r.volume_basis == "full_session"

    def test_prior_day_reaction_unaffected_by_scan_time(self):
        # Day-1 drift entries (the common PEAD case) read a completed bar, so
        # an intraday scan must NOT downgrade them to partial.
        from datetime import datetime
        from zoneinfo import ZoneInfo

        closes = [100.0] * 25 + [110.0, 111.0]
        df = make_df(closes)
        report_day = df.index[25].date()
        today = df.index[26].date()
        midsession = datetime(today.year, today.month, today.day, 10, 5,
                              tzinfo=ZoneInfo("America/New_York"))
        r = reaction_metrics("T", df, report_day, "bmo", now=midsession)
        assert r.volume_basis == "full_session"

    def test_amc_reaction_next_day(self):
        closes = [100.0] * 25 + [100.0, 112.0]
        df = make_df(closes)
        report_day = df.index[25].date()  # reports after this close
        r = reaction_metrics("T", df, report_day, "amc")
        assert r.reaction_date == df.index[26].date().isoformat()
        assert r.move_pct == 12.0

    def test_amc_before_market_opens_returns_none(self):
        closes = [100.0] * 26
        df = make_df(closes)
        report_day = df.index[25].date()  # last bar IS report day
        assert reaction_metrics("T", df, report_day, "amc") is None

    def test_unknown_timing_picks_bigger_move(self):
        # Day D: +1%; day D+1: +9% -> reaction must be D+1.
        closes = [100.0] * 25 + [101.0, 110.0]
        df = make_df(closes)
        report_day = df.index[25].date()
        r = reaction_metrics("T", df, report_day, "unknown")
        assert r.reaction_date == df.index[26].date().isoformat()

    def test_unknown_timing_waits_for_next_session(self):
        # Report day IS the last bar and timing is unknown, so the report may
        # have landed after the close -- that day predates the news and must
        # not be read as confirmation. (2026-08-04 ADEA false positive.)
        closes = [100.0] * 25 + [104.28]
        df = make_df(closes)
        report_day = df.index[25].date()
        assert reaction_metrics("T", df, report_day, "unknown") is None

    def test_unknown_timing_non_trading_report_date(self):
        # Report dated on a weekend day: the next session reacts to it whatever
        # the timing was, even with no bar strictly after that session yet.
        closes = [100.0] * 25 + [109.0]
        df = make_df(closes)
        dates = [d.date() for d in df.index]
        # Last bar follows a weekend gap, so truncate to end on that session.
        gap = max(i for i in range(1, len(dates)) if (dates[i] - dates[i - 1]).days > 1)
        df = df.iloc[: gap + 1]
        df.iloc[gap, df.columns.get_loc("Close")] = 109.0
        non_trading_day = dates[gap - 1] + timedelta(days=1)
        r = reaction_metrics("T", df, non_trading_day, "unknown")
        assert r.reaction_date == dates[gap].isoformat()
        assert r.move_pct == 9.0

    def test_drift_since_reaction(self):
        closes = [100.0] * 25 + [110.0, 111.0, 113.3]
        df = make_df(closes)
        r = reaction_metrics("T", df, df.index[25].date(), "bmo")
        assert r.drift_since_pct == 3.0
        assert r.days_since_reaction == 2

    def test_report_before_history_returns_none(self):
        df = make_df([100.0] * 10)
        assert reaction_metrics("T", df, df.index[0].date(), "bmo") is None


FLOORS = {"min_avg_dollar_volume": 5_000_000, "min_price": 5.0,
          "min_reaction_move_pct": 3.0}


def make_reaction(**overrides):
    from autoswing.data.prices import Reaction
    base = dict(
        symbol="T", reaction_date="2026-07-08", prior_close=100.0,
        gap_pct=6.0, move_pct=8.0, drift_since_pct=1.0, volume_ratio=4.0,
        adv_dollar_20d=50_000_000.0, last_close=108.0, days_since_reaction=1,
    )
    base.update(overrides)
    return Reaction(**base)


def make_report(**overrides):
    base = dict(
        symbol="T", report_date="2026-07-07", timing="amc",
        eps_actual=1.0, eps_forecast=0.8, surprise_pct=25.0,
        num_estimates=5, market_cap=2e9, company="Test Co",
    )
    base.update(overrides)
    return Report(**base)


class TestCandidateFloors:
    def test_clean_candidate_passes(self):
        c = build_candidate(make_report(), make_reaction(), FLOORS)
        assert c["rejects"] == []

    def test_no_reaction_yet(self):
        c = build_candidate(make_report(), None, FLOORS)
        assert c["rejects"] == ["no_reaction_data_yet"]

    def test_illiquid_rejected(self):
        c = build_candidate(make_report(), make_reaction(adv_dollar_20d=1e6), FLOORS)
        assert any("illiquid" in r for r in c["rejects"])

    def test_cheap_stock_rejected(self):
        c = build_candidate(
            make_report(), make_reaction(last_close=3.5), FLOORS
        )
        assert any("price" in r for r in c["rejects"])

    def test_small_move_rejected(self):
        c = build_candidate(make_report(), make_reaction(move_pct=1.2), FLOORS)
        assert any("too small" in r for r in c["rejects"])

    def test_negative_reaction_rejected_long_only(self):
        # Sold-off beats and misses (ALV/RYAAY/BFC pattern) are not long
        # candidates; the scan now rejects them instead of the brain.
        c = build_candidate(make_report(), make_reaction(move_pct=-8.6), FLOORS)
        assert any("long-only" in r for r in c["rejects"])

    def test_failed_price_fetch_distinguished_from_not_yet_traded(self):
        # 2026-08-05: a dropped batch download was labelled no_reaction_data_yet,
        # so "we couldn't fetch it" read as "re-check tomorrow" and candidates
        # (ATKR, IBTA) silently vanished between identical scans.
        c = build_candidate(make_report(), None, FLOORS, has_prices=False)
        assert c["rejects"] == ["price_data_unavailable"]


class TestFetchHistoryRetry:
    """A partial batch miss is retried; a wholesale miss is not."""

    def _patch(self, monkeypatch, responses):
        calls = []

        def fake_download(symbols, **kwargs):
            calls.append(list(symbols))
            return responses.pop(0)

        import yfinance as yf
        monkeypatch.setattr(yf, "download", fake_download)
        return calls

    def _frame(self):
        import pandas as pd
        idx = pd.to_datetime(["2026-07-07", "2026-07-08"])
        return pd.DataFrame(
            {"Open": [100.0, 106.0], "Close": [100.0, 108.0],
             "Volume": [1e6, 4e6]}, index=idx,
        )

    def test_partial_miss_is_retried_and_recovered(self, monkeypatch):
        import pandas as pd
        from autoswing.data.prices import fetch_history

        df = self._frame()
        first = pd.concat({"A": df, "B": df, "C": df}, axis=1)   # D dropped
        second = pd.concat({"D": df}, axis=1)
        calls = self._patch(monkeypatch, [first, second])

        out = fetch_history(["A", "B", "C", "D"])
        assert calls == [["A", "B", "C", "D"], ["D"]]
        assert sorted(out) == ["A", "B", "C", "D"]

    def test_wholesale_miss_is_not_retried(self, monkeypatch):
        import pandas as pd
        from autoswing.data.prices import fetch_history

        df = self._frame()
        first = pd.concat({"A": df}, axis=1)  # 3 of 4 missing -> real outage
        calls = self._patch(monkeypatch, [first])

        out = fetch_history(["A", "B", "C", "D"])
        assert calls == [["A", "B", "C", "D"]]  # no second call
        assert sorted(out) == ["A"]


class TestSameDayBmoStaleness:
    """Family instance #7 (HTHT 2026-08-17): a same-day report that already
    printed before the open must not read as 'upcoming'."""

    def _resolve(self, known, stamped, now):
        from autoswing.data.earnings import ET, resolve_next_earnings
        return resolve_next_earnings(known, stamped, now.replace(tzinfo=ET))

    def test_bmo_already_printed_is_not_upcoming(self):
        from datetime import date, datetime
        from autoswing.data.earnings import ET
        today = date(2026, 8, 17)
        bmo = datetime(2026, 8, 17, 8, 0, tzinfo=ET)
        # 10:00 ET entry window: the 08:00 print is behind us -> derive "none"
        assert self._resolve([today], [bmo], datetime(2026, 8, 17, 10, 0)) == "none"

    def test_bmo_not_yet_printed_stays_upcoming(self):
        from datetime import date, datetime
        from autoswing.data.earnings import ET
        today = date(2026, 8, 17)
        bmo = datetime(2026, 8, 17, 8, 0, tzinfo=ET)
        # 07:30 ET premarket: still ahead -> upcoming today (blackout applies)
        assert self._resolve([today], [bmo], datetime(2026, 8, 17, 7, 30)) == "2026-08-17"

    def test_amc_same_day_stays_upcoming_all_session(self):
        from datetime import date, datetime
        from autoswing.data.earnings import ET
        today = date(2026, 8, 17)
        amc = datetime(2026, 8, 17, 16, 0, tzinfo=ET)
        assert self._resolve([today], [amc], datetime(2026, 8, 17, 10, 0)) == "2026-08-17"

    def test_unstamped_same_day_stays_upcoming(self):
        # no timestamp = can't tell = never guess it's already out
        from datetime import date, datetime
        today = date(2026, 8, 17)
        assert self._resolve([today], [], datetime(2026, 8, 17, 14, 0)) == "2026-08-17"

    def test_reported_bmo_yields_to_real_future_date(self):
        from datetime import date, datetime
        from autoswing.data.earnings import ET
        today = date(2026, 8, 17)
        bmo = datetime(2026, 8, 17, 8, 0, tzinfo=ET)
        nxt = date(2026, 11, 16)
        assert self._resolve([today, nxt], [bmo], datetime(2026, 8, 17, 10, 0)) == "2026-11-16"

    def test_old_report_beyond_30d_is_unknown(self):
        from datetime import date, datetime
        assert self._resolve([date(2026, 5, 15)], [], datetime(2026, 8, 17, 10, 0)) == "unknown"


class TestQualityFlags:
    """Data-quality flags label a misleading headline surprise; they must
    never reject a candidate, and must stay quiet on clean rows."""

    def row(self, **kw):
        base = {"symbol": "T", "eps": "$1.55", "epsForecast": "$1.48",
                "surprise": "4.7", "noOfEsts": "12", "time": "time-pre-market"}
        base.update(kw)
        return parse_calendar_rows([base], date(2026, 8, 20))[0]

    def test_clean_row_has_no_flags(self):
        assert self.row().quality_flags == []

    def test_duot_case_all_three_flags(self):
        # 2026-08-20: forecast -$0.02, reported -$0.13, 1 estimate -> -550%,
        # while the real print was +$1.61 incl. a $53.2M asset-sale gain.
        r = self.row(eps="($0.13)", epsForecast="($0.02)", surprise="-550",
                     noOfEsts="1")
        assert set(r.quality_flags) == {
            "thin_coverage", "tiny_denominator", "extreme_surprise_ratio"}

    def test_thin_coverage_alone(self):
        assert self.row(noOfEsts="2").quality_flags == ["thin_coverage"]

    def test_sign_flip_flagged(self):
        # loss expected, profit delivered: the % is real but the framing misleads
        r = self.row(eps="$0.40", epsForecast="($0.20)", surprise="300")
        assert "sign_flip" in r.quality_flags

    def test_surprise_inconsistent_with_its_own_operands(self):
        # feed says +50% but (1.55-1.48)/1.48 is +4.7%
        assert "surprise_inconsistent" in self.row(surprise="50").quality_flags

    def test_incomplete_eps_short_circuits(self):
        r = self.row(eps="N/A")
        assert "incomplete_eps" in r.quality_flags
        assert "tiny_denominator" not in r.quality_flags

    def test_flags_never_reject(self):
        # build_candidate must not turn a flag into a rejection
        from autoswing.data.earnings import quality_flags
        rep = make_report(num_estimates=1, eps_forecast=-0.02, eps_actual=-0.13,
                          surprise_pct=-550.0)
        rep.quality_flags = quality_flags(rep)
        c = build_candidate(rep, make_reaction(), FLOORS)
        assert c["rejects"] == []
        assert set(rep.quality_flags) <= set(c["quality_flags"])


class TestReactionContradictsSurprise:
    """GTLB 2026-09-02: the feed graded a street-adjusted +33% beat as a
    -85.7% GAAP miss while the stock gapped +22% on 2x volume — a
    consensus-basis mismatch only a manual news check caught. The market
    voting hard against the graded surprise is now a structural label."""

    def test_big_miss_with_big_pop_flagged(self):
        c = build_candidate(
            make_report(eps_actual=-0.13, eps_forecast=-0.07, surprise_pct=-85.7),
            make_reaction(move_pct=14.9), FLOORS,
        )
        assert "reaction_contradicts_surprise" in c["quality_flags"]

    def test_big_beat_sold_off_flagged(self):
        # Rejected long-only anyway, but the label must still be honest.
        c = build_candidate(make_report(surprise_pct=40.0),
                            make_reaction(move_pct=-8.0), FLOORS)
        assert "reaction_contradicts_surprise" in c["quality_flags"]

    def test_aligned_surprise_and_reaction_not_flagged(self):
        c = build_candidate(make_report(), make_reaction(), FLOORS)
        assert "reaction_contradicts_surprise" not in c["quality_flags"]

    def test_modest_surprise_not_flagged(self):
        # An ordinary miss bought on guidance is not a basis mismatch.
        c = build_candidate(make_report(surprise_pct=-10.0),
                            make_reaction(move_pct=8.0), FLOORS)
        assert "reaction_contradicts_surprise" not in c["quality_flags"]

    def test_small_reaction_not_flagged(self):
        c = build_candidate(make_report(surprise_pct=-85.7),
                            make_reaction(move_pct=1.0), FLOORS)
        assert "reaction_contradicts_surprise" not in c["quality_flags"]

    def test_missing_surprise_tolerated(self):
        c = build_candidate(make_report(surprise_pct=None),
                            make_reaction(move_pct=14.9), FLOORS)
        assert "reaction_contradicts_surprise" not in c["quality_flags"]

    def test_no_reaction_tolerated(self):
        c = build_candidate(make_report(surprise_pct=-85.7), None, FLOORS)
        assert "reaction_contradicts_surprise" not in c["quality_flags"]

    def test_report_flags_not_mutated(self):
        r = make_report(surprise_pct=-85.7)
        build_candidate(r, make_reaction(move_pct=14.9), FLOORS)
        assert r.quality_flags == []
