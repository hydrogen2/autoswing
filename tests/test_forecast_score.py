"""Regression: forecast scoring must not depend on Nasdaq backfilling
actuals into its calendar day-rows.

On 2026-09-16 three 09-10 forecasts (DSGX, HUBG, LPTH) hit grace expiry
and burned as eps_actual "unknown". DSGX and LPTH had really reported on
schedule with public numbers — the Nasdaq day-rows simply never gained
`eps`/`surprise` after the print. The calendar is a pre-print source; at
scoring time the per-symbol yfinance history (reported EPS + estimate)
is the reliable one. HUBG was the other failure mode — a phantom
calendar date with no real print nearby — and must still burn honestly
as unknown rather than be guessed.
"""

import json
from datetime import date, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

import autoswing.data.earnings as earnings
import autoswing.data.prices as prices
from autoswing.commands import research
from autoswing.data.earnings import Report, reported_surprise
from autoswing.journal import Journal


def _forecast_row(symbol: str, report_date: date, eps_call: str = "beat"):
    return {
        "id": f"{symbol}-{report_date.isoformat()}",
        "symbol": symbol,
        "made_at": "2026-09-09T12:00:00+00:00",
        "report_date": report_date.isoformat(),
        "timing": "amc",
        "tier": "quick",
        "eps_call": eps_call,
        "reaction_call": None,
        "confidence": 0.6,
        "reasoning": "test",
    }


def _run_score(tmp_path, monkeypatch, forecast, *, calendar_reports,
               fallback_surprise, move_pct=6.6, surprise_fn=None):
    fpath = tmp_path / "forecasts.jsonl"
    spath = tmp_path / "scores.jsonl"
    fpath.write_text(json.dumps(forecast) + "\n")
    monkeypatch.setattr(research, "_forecast_paths", lambda: (fpath, spath))
    monkeypatch.setattr(earnings, "fetch_calendar_day",
                        lambda day, session=None: calendar_reports)
    monkeypatch.setattr(
        earnings, "reported_surprise",
        surprise_fn or (lambda symbol, rdate, window_days=2,
                        fallback_estimate=None: fallback_surprise))
    monkeypatch.setattr(prices, "fetch_history",
                        lambda symbols, period="1mo": {forecast["symbol"]: object()})
    monkeypatch.setattr(
        prices, "reaction_metrics",
        lambda symbol, df, rdate, timing, now=None:
            None if move_pct is None else SimpleNamespace(move_pct=move_pct))
    out = research._forecast_score(Journal(tmp_path / "journal"))
    scores = [json.loads(l) for l in spath.read_text().splitlines()] \
        if spath.exists() else []
    return out, scores


class TestScoringFallback:
    def test_yfinance_fallback_rescues_missing_calendar_actuals(
            self, tmp_path, monkeypatch):
        # DSGX shape: no usable day-row six days after a real print.
        rdate = date.today() - timedelta(days=7)
        fc = _forecast_row("DSGX", rdate, eps_call="beat")
        out, scores = _run_score(tmp_path, monkeypatch, fc,
                                 calendar_reports=[], fallback_surprise=12.0)
        assert out["scored"] == 1
        assert scores[0]["eps_actual"] == "beat"
        assert scores[0]["eps_correct"] is True
        assert scores[0]["surprise_pct"] == 12.0

    def test_day_row_without_surprise_derives_from_its_eps_fields(
            self, tmp_path, monkeypatch):
        rdate = date.today() - timedelta(days=7)
        fc = _forecast_row("LPTH", rdate, eps_call="beat")
        row = Report(symbol="LPTH", report_date=rdate.isoformat(),
                     timing="amc", eps_actual=0.60, eps_forecast=0.50,
                     surprise_pct=None, num_estimates=2, market_cap=None)

        def boom(symbol, rdate, window_days=2):  # pragma: no cover
            raise AssertionError("fallback must not fire when the row derives")

        monkeypatch.setattr(earnings, "reported_surprise", boom)
        out, scores = _run_score(
            tmp_path, monkeypatch, fc,
            calendar_reports=[row], fallback_surprise=None)
        assert out["scored"] == 1
        assert scores[0]["surprise_pct"] == pytest.approx(20.0)
        assert scores[0]["eps_actual"] == "beat"

    def test_grace_open_still_defers_when_no_source_has_actuals(
            self, tmp_path, monkeypatch):
        rdate = date.today() - timedelta(days=2)
        fc = _forecast_row("HUBG", rdate)
        out, scores = _run_score(tmp_path, monkeypatch, fc,
                                 calendar_reports=[], fallback_surprise=None)
        assert out["scored"] == 0
        assert out["awaiting_actuals"] == 1
        assert scores == []

    def test_grace_expired_phantom_date_burns_as_unknown_not_a_guess(
            self, tmp_path, monkeypatch):
        # HUBG shape: calendar artifact date, no real print in any source.
        rdate = date.today() - timedelta(days=7)
        fc = _forecast_row("HUBG", rdate, eps_call="inline")
        out, scores = _run_score(tmp_path, monkeypatch, fc,
                                 calendar_reports=[], fallback_surprise=None)
        assert out["scored"] == 1
        assert scores[0]["eps_actual"] == "unknown"
        assert scores[0]["eps_correct"] is None

    def test_stored_consensus_reaches_the_fallback(self, tmp_path, monkeypatch):
        # NB 2026-09-11: single-analyst microcap whose calendar row was
        # gone by scoring time; the consensus captured at log time is the
        # estimate basis the yfinance fallback needs.
        rdate = date.today() - timedelta(days=6)
        fc = _forecast_row("NB", rdate, eps_call="miss")
        fc["eps_consensus"] = -0.03
        seen = {}

        def fake_surprise(symbol, rdate, window_days=2, fallback_estimate=None):
            seen["basis"] = fallback_estimate
            return -533.3

        out, scores = _run_score(tmp_path, monkeypatch, fc,
                                 calendar_reports=[], fallback_surprise=None,
                                 surprise_fn=fake_surprise)
        assert seen["basis"] == pytest.approx(-0.03)
        assert scores[0]["eps_actual"] == "miss"
        assert scores[0]["eps_correct"] is True

    def test_live_calendar_consensus_is_the_second_basis(
            self, tmp_path, monkeypatch):
        # Rows logged before eps_consensus existed: a still-live calendar
        # row's estimate serves as the basis instead.
        rdate = date.today() - timedelta(days=6)
        fc = _forecast_row("NB", rdate, eps_call="miss")
        row = Report(symbol="NB", report_date=rdate.isoformat(),
                     timing="unknown", eps_actual=None, eps_forecast=-0.03,
                     surprise_pct=None, num_estimates=1, market_cap=None)
        seen = {}

        def fake_surprise(symbol, rdate, window_days=2, fallback_estimate=None):
            seen["basis"] = fallback_estimate
            return -533.3

        _run_score(tmp_path, monkeypatch, fc,
                   calendar_reports=[row], fallback_surprise=None,
                   surprise_fn=fake_surprise)
        assert seen["basis"] == pytest.approx(-0.03)


def _fake_yf(monkeypatch, df, raises=False):
    import yfinance

    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        def get_earnings_dates(self, limit=12):
            if raises:
                raise RuntimeError("rate limited")
            return df

    monkeypatch.setattr(yfinance, "Ticker", FakeTicker)


def _history(rows):  # rows: [(iso_ts, estimate, reported)]
    return pd.DataFrame(
        {"EPS Estimate": [r[1] for r in rows],
         "Reported EPS": [r[2] for r in rows]},
        index=pd.DatetimeIndex([r[0] for r in rows], tz="America/New_York"))


class TestReportedSurprise:
    def test_derives_percent_from_row_within_window(self, monkeypatch):
        _fake_yf(monkeypatch, _history([
            ("2026-09-10 16:05:00", 0.44, 0.47),
            ("2026-06-03 16:05:00", 0.40, 0.41),
        ]))
        got = reported_surprise("DSGX", date(2026, 9, 10))
        assert got == pytest.approx(100.0 * (0.47 - 0.44) / 0.44)

    def test_feed_date_off_by_one_still_matches(self, monkeypatch):
        _fake_yf(monkeypatch, _history([("2026-09-11 09:00:00", 0.50, 0.60)]))
        assert reported_surprise("LPTH", date(2026, 9, 10)) == pytest.approx(20.0)

    def test_no_row_within_window_returns_none(self, monkeypatch):
        _fake_yf(monkeypatch, _history([("2026-10-28 16:05:00", 0.52, 0.55)]))
        assert reported_surprise("HUBG", date(2026, 9, 10)) is None

    def test_unpublished_reported_eps_returns_none(self, monkeypatch):
        _fake_yf(monkeypatch, _history([
            ("2026-09-10 16:05:00", 0.44, float("nan"))]))
        assert reported_surprise("DSGX", date(2026, 9, 10)) is None

    def test_zero_estimate_cannot_make_a_percent(self, monkeypatch):
        _fake_yf(monkeypatch, _history([("2026-09-10 16:05:00", 0.0, 0.10)]))
        assert reported_surprise("X", date(2026, 9, 10)) is None

    def test_fetch_failure_returns_none(self, monkeypatch):
        _fake_yf(monkeypatch, None, raises=True)
        assert reported_surprise("DSGX", date(2026, 9, 10)) is None

    def test_none_history_returns_none(self, monkeypatch):
        _fake_yf(monkeypatch, None)
        assert reported_surprise("DSGX", date(2026, 9, 10)) is None

    def test_nan_estimate_uses_fallback_consensus(self, monkeypatch):
        # NB 2026-09-11: Yahoo carried the real ~-$0.19 print but no
        # estimate (single analyst); the row was skipped and the leg
        # burned as "unknown". The consensus the call was made against
        # stands in for the missing provider estimate.
        _fake_yf(monkeypatch, _history([
            ("2026-09-09 08:00:00", float("nan"), -0.19)]))
        got = reported_surprise("NB", date(2026, 9, 11),
                                fallback_estimate=-0.03)
        assert got == pytest.approx(100.0 * (-0.19 + 0.03) / 0.03)

    def test_nan_estimate_without_fallback_still_none(self, monkeypatch):
        _fake_yf(monkeypatch, _history([
            ("2026-09-09 08:00:00", float("nan"), -0.19)]))
        assert reported_surprise("NB", date(2026, 9, 11)) is None

    def test_provider_estimate_beats_fallback(self, monkeypatch):
        _fake_yf(monkeypatch, _history([("2026-09-10 16:05:00", 0.44, 0.47)]))
        got = reported_surprise("DSGX", date(2026, 9, 10),
                                fallback_estimate=1.0)
        assert got == pytest.approx(100.0 * (0.47 - 0.44) / 0.44)

    def test_fallback_never_replaces_missing_reported(self, monkeypatch):
        # The estimate may be substituted; the reported number never is.
        _fake_yf(monkeypatch, _history([
            ("2026-09-10 16:05:00", 0.44, float("nan"))]))
        assert reported_surprise("DSGX", date(2026, 9, 10),
                                 fallback_estimate=0.44) is None


class TestLogCapturesConsensus:
    """The scorer can only use a consensus basis that still exists at
    scoring time; capture it when the forecast is logged, while the
    calendar row is live. Capture is best-effort — a lookup failure must
    never block the log."""

    def _log(self, tmp_path, monkeypatch, payload, calendar):
        fpath = tmp_path / "forecasts.jsonl"
        spath = tmp_path / "scores.jsonl"
        monkeypatch.setattr(research, "_forecast_paths",
                            lambda: (fpath, spath))
        monkeypatch.setattr(earnings, "fetch_calendar_day", calendar)
        pfile = tmp_path / "payload.json"
        pfile.write_text(json.dumps(payload))
        research._forecast_log(SimpleNamespace(forecast=str(pfile)),
                               Journal(tmp_path / "journal"))
        return json.loads(fpath.read_text().splitlines()[0])

    def _payload(self, **kw):
        rd = (date.today() + timedelta(days=2)).isoformat()
        base = {"symbol": "NB", "report_date": rd, "timing": "unknown",
                "tier": "quick", "eps_call": "miss", "confidence": 0.55,
                "reasoning": "test"}
        base.update(kw)
        return base

    def _row(self, eps_forecast):
        rd = (date.today() + timedelta(days=2)).isoformat()
        return Report(symbol="NB", report_date=rd, timing="unknown",
                      eps_actual=None, eps_forecast=eps_forecast,
                      surprise_pct=None, num_estimates=1, market_cap=None)

    def test_consensus_captured_from_calendar_row(self, tmp_path, monkeypatch):
        got = self._log(tmp_path, monkeypatch, self._payload(),
                        lambda day, session=None: [self._row(-0.03)])
        assert got["eps_consensus"] == pytest.approx(-0.03)

    def test_explicit_payload_value_wins(self, tmp_path, monkeypatch):
        got = self._log(tmp_path, monkeypatch,
                        self._payload(eps_consensus=-0.05),
                        lambda day, session=None: [self._row(-0.03)])
        assert got["eps_consensus"] == pytest.approx(-0.05)

    def test_lookup_failure_never_blocks_the_log(self, tmp_path, monkeypatch):
        def boom(day, session=None):
            raise RuntimeError("nasdaq down")
        got = self._log(tmp_path, monkeypatch, self._payload(), boom)
        assert got["eps_consensus"] is None
        assert got["id"].startswith("NB-")
