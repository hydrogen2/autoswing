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
