"""Instrument #8 read: insider cluster-buying vs fwd-15d drift.

The verdict bar was pre-registered 2026-10-23 and owner-approved 2026-10-27
BEFORE the first read; these tests pin the bar so no later edit can soften
it without tripping something.
"""

import json

import pandas as pd

from autoswing.research import (
    INSIDER_MIN_N_FOR_VERDICT,
    INSIDER_PREMIUM_PP,
    extract_insider_rows,
    insider_outcomes_verdict,
    score_insider_outcomes,
)


def scan_line(ts, insider_buying):
    return json.dumps({"ts": ts, "event": "data.scan_candidates",
                       "scanned": 1, "passing": len(insider_buying),
                       "insider_buying": insider_buying})


AVAILABLE = {"buys": 3, "distinct_insiders": 2, "cluster": True,
             "total_shares": 1000.0, "est_notional_usd": 60000}
NO_BUYS = {"buys": 0, "distinct_insiders": 0, "cluster": False,
           "total_shares": 0.0, "est_notional_usd": 0}


class TestExtract:
    def test_first_available_summary_of_a_day_stands(self, tmp_path):
        later = dict(AVAILABLE, buys=9, cluster=False)
        (tmp_path / "2026-10-01.jsonl").write_text(
            scan_line("2026-10-01T12:00:00+00:00", {"AAA": AVAILABLE}) + "\n"
            + scan_line("2026-10-01T15:00:00+00:00", {"AAA": later}) + "\n")
        out = extract_insider_rows(tmp_path)
        assert len(out["rows"]) == 1
        assert out["rows"][0]["buys"] == 3 and out["rows"][0]["cluster"]

    def test_unavailable_replaced_by_later_available_same_day(self, tmp_path):
        (tmp_path / "2026-10-01.jsonl").write_text(
            scan_line("2026-10-01T12:00:00+00:00",
                      {"AAA": {"unavailable": "EDGAR 503"}}) + "\n"
            + scan_line("2026-10-01T15:00:00+00:00", {"AAA": NO_BUYS}) + "\n")
        out = extract_insider_rows(tmp_path)
        assert len(out["rows"]) == 1 and out["rows"][0]["cluster"] is False
        assert out["unavailable"] == 0

    def test_never_resolved_is_counted_not_dropped(self, tmp_path):
        (tmp_path / "2026-10-01.jsonl").write_text(
            scan_line("2026-10-01T12:00:00+00:00",
                      {"AAA": {"unavailable": "no unambiguous CIK"},
                       "BBB": NO_BUYS}) + "\n")
        out = extract_insider_rows(tmp_path)
        assert [r["symbol"] for r in out["rows"]] == ["BBB"]
        assert out["unavailable"] == 1
        assert out["unavailable_symbol_days"] == ["AAA-2026-10-01"]

    def test_same_symbol_on_two_days_is_two_rows(self, tmp_path):
        (tmp_path / "2026-10-01.jsonl").write_text(
            scan_line("2026-10-01T12:00:00+00:00", {"AAA": AVAILABLE}) + "\n")
        (tmp_path / "2026-10-02.jsonl").write_text(
            scan_line("2026-10-02T12:00:00+00:00", {"AAA": AVAILABLE}) + "\n")
        out = extract_insider_rows(tmp_path)
        assert len(out["rows"]) == 2


def price_df(n_days, start="2026-09-01", daily=1.0, base=100.0):
    idx = pd.bdate_range(start, periods=n_days)
    closes = [base + daily * i for i in range(n_days)]
    return pd.DataFrame({"Open": closes, "High": closes, "Low": closes,
                         "Close": closes, "Volume": 1_000_000}, index=idx)


def row(symbol="AAA", d="2026-09-01", cluster=False, buys=0, notional=0.0):
    return {"symbol": symbol, "date": d, "cluster": cluster, "buys": buys,
            "distinct_insiders": 2 if cluster else min(buys, 1),
            "est_notional_usd": notional}


class TestScoring:
    def test_full_15_sessions_required(self):
        hist = {"AAA": price_df(16), "BBB": price_df(15)}
        out = score_insider_outcomes(
            [row("AAA"), row("BBB", cluster=True, buys=2)], hist)
        assert out["measured"] == 1 and out["pending"] == 1
        # 100 -> 115 over 15 sessions
        assert out["rows"][0]["fwd_15d_pct"] == 15.0

    def test_missing_prices_reported_not_pending(self):
        out = score_insider_outcomes([row("ZZZ")], {})
        assert out["measured"] == 0 and out["pending"] == 0
        assert out["price_data_missing"] == ["ZZZ-2026-09-01"]

    def test_notional_buckets(self):
        hist = {"AAA": price_df(16)}
        out = score_insider_outcomes(
            [row("AAA", buys=1, notional=40000)], hist)
        assert out["rows"][0]["notional_bucket"] == "<50k"
        assert "no_buys" not in out["by_notional_bucket"]


def measured(n_cluster, cluster_fwd, n_non, non_fwd, best=None):
    rows = [{"cluster": True, "fwd_15d_pct": cluster_fwd}
            for _ in range(n_cluster)]
    rows += [{"cluster": False, "fwd_15d_pct": non_fwd} for _ in range(n_non)]
    if best is not None:
        rows[0] = {"cluster": True, "fwd_15d_pct": best}
    return rows


class TestVerdict:
    def test_bar_constants_are_the_registered_ones(self):
        assert INSIDER_MIN_N_FOR_VERDICT == 100
        assert INSIDER_PREMIUM_PP == 2.0

    def test_pending_below_100(self):
        v = insider_outcomes_verdict(measured(10, 5.0, 89, 1.0))
        assert v["verdict"] == "pending" and v["remaining"] == 1
        assert v["separation_pp"] == 4.0

    def test_premium_when_gap_survives_best_drop(self):
        v = insider_outcomes_verdict(measured(20, 4.0, 80, 1.0))
        assert v["verdict"] == "cluster_premium"
        assert v["separation_after_best_drop_pp"] == 3.0

    def test_no_premium_when_one_outlier_carries_the_gap(self):
        # 19 cluster rows at 1.0 plus one at 60: mean clears the bar only
        # because of the best row; dropping it must kill the verdict.
        v = insider_outcomes_verdict(measured(20, 1.0, 80, 1.0, best=60.0))
        assert v["separation_pp"] >= INSIDER_PREMIUM_PP
        assert v["verdict"] == "no_cluster_premium"

    def test_no_premium_when_no_cluster_rows_at_n(self):
        v = insider_outcomes_verdict(measured(0, 0.0, 100, 1.0))
        assert v["verdict"] == "no_cluster_premium"
        assert v["separation_pp"] is None

    def test_single_cluster_row_cannot_pass(self):
        # One cluster row means the best-drop leg is undefined -> fail,
        # never pass on an unreplicated outlier.
        v = insider_outcomes_verdict(measured(1, 50.0, 99, 1.0))
        assert v["verdict"] == "no_cluster_premium"
        assert v["separation_after_best_drop_pp"] is None
