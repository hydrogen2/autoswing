"""Hedged-book shadow line: fixed-ratio arithmetic, the in-sample/forward
split, and failure modes that must not pass for a quiet day."""

import json

import pytest

import autoswing.hedge as hedge
from autoswing.hedge import (
    daily_returns, hedged_book, hedged_book_safe, load_marks, window_stats,
)


def write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def mark(d, equity, bench):
    return {"date": d, "virtual_equity": equity, "benchmark_close": bench}


class TestRegistration:
    def test_pinned(self):
        # Never re-fit: changing these is a dated, deliberate edit that also
        # edits this test.
        assert hedge.HEDGE_RATIO == 0.5
        assert hedge.HEDGE_REGISTERED == "2026-11-10"
        assert hedge.HEDGE_INSAMPLE_BASE == "2026-07-24"
        assert hedge.HEDGE_FORWARD_BASE == "2026-11-09"


class TestMarks:
    def test_dedupes_last_wins_and_sorts(self, tmp_path):
        p = write(tmp_path / "b.jsonl", [
            mark("2026-11-11", 102.0, 50.0),
            mark("2026-11-10", 100.0, 50.0),
            mark("2026-11-11", 103.0, 51.0),      # corrected re-mark
        ])
        marks = load_marks(p)
        assert [m["date"] for m in marks] == ["2026-11-10", "2026-11-11"]
        assert marks[1]["virtual_equity"] == 103.0

    def test_unusable_rows_dropped(self, tmp_path):
        p = write(tmp_path / "b.jsonl", [
            mark("2026-11-10", 100.0, 50.0),
            {"date": "2026-11-11", "virtual_equity": 101.0},          # no bench
            {"date": "2026-11-12", "virtual_equity": 0, "benchmark_close": 50},
            mark("2026-11-13", 102.0, 51.0),
        ])
        assert [m["date"] for m in load_marks(p)] == ["2026-11-10", "2026-11-13"]

    def test_missing_file_is_empty(self, tmp_path):
        assert load_marks(tmp_path / "nope.jsonl") == []

    def test_gap_folds_into_next_mark_for_both_series(self):
        rs = daily_returns([mark("2026-11-10", 100.0, 50.0),
                            mark("2026-11-13", 110.0, 55.0)])
        assert rs == [("2026-11-13", pytest.approx(0.10), pytest.approx(0.10))]


class TestWindowStats:
    def test_hedged_is_bot_minus_half_benchmark_compounded(self):
        rs = [("d1", 0.02, 0.04), ("d2", -0.01, -0.02), ("d3", 0.00, 0.03)]
        w = window_stats(rs, 0.5)
        # hedged days: 0.00, 0.00, -0.015
        assert w["hedged"]["return_pct"] == pytest.approx(-1.5)
        assert w["hedged"]["worst_day_pct"] == pytest.approx(-1.5)
        assert w["unhedged"]["return_pct"] == pytest.approx(
            100 * (1.02 * 0.99 - 1), abs=0.01)
        assert w["n_days"] == 3 and w["through"] == "d3"

    def test_drawdown_is_peak_to_trough_from_window_start(self):
        rs = [("d1", -0.10, 0.0), ("d2", 0.05, 0.0), ("d3", -0.10, 0.0)]
        # 1.0 -> 0.90 -> 0.945 -> 0.8505; peak stays at the window start
        assert window_stats(rs, 0.5)["unhedged"]["max_drawdown_pct"] == \
            pytest.approx(14.95)

    def test_down_days_average_only_benchmark_stress_days(self):
        rs = [("d1", -0.02, -0.03), ("d2", 0.01, 0.01), ("d3", -0.01, -0.01)]
        dd = window_stats(rs, 0.5)["benchmark_down_days"]
        assert dd["n"] == 2                      # -3% and exactly -1%
        assert dd["unhedged_avg_pct"] == pytest.approx(-1.5)
        assert dd["hedged_avg_pct"] == pytest.approx(-0.5)
        assert dd["benchmark_avg_pct"] == pytest.approx(-2.0)

    def test_no_down_days_reports_zero_not_an_average(self):
        dd = window_stats([("d1", 0.01, 0.01)], 0.5)["benchmark_down_days"]
        assert dd == {"threshold_pct": -1.0, "n": 0}

    def test_beta_withheld_on_a_short_window(self):
        w = window_stats([("d", 0.01, 0.02)] * 5, 0.5)
        assert w["realized_beta"] is None and w["realized_corr"] is None

    def test_beta_of_a_half_beta_book(self):
        rs = [(f"d{i}", 0.5 * x, x)
              for i, x in enumerate([0.01, -0.02, 0.03, -0.01] * 6)]
        w = window_stats(rs, 0.5)
        assert w["realized_beta"] == pytest.approx(0.5)
        assert w["realized_corr"] == pytest.approx(1.0)
        # and the fixed hedge takes all of it out
        assert w["hedged"]["return_pct"] == pytest.approx(0.0)
        assert w["hedged"]["max_drawdown_pct"] == pytest.approx(0.0)

    def test_flat_series_does_not_divide_by_zero(self):
        w = window_stats([("d", 0.0, 0.0)] * 25, 0.5)
        assert w["realized_beta"] is None
        assert w["hedged"]["ann_vol_pct"] == 0.0

    def test_empty_window(self):
        assert window_stats([], 0.5) == {"n_days": 0}


class TestHedgedBook:
    def rows(self):
        return [
            mark("2026-07-23", 90.0, 45.0),     # before the in-sample base
            mark("2026-07-24", 100.0, 50.0),    # in-sample base mark
            mark("2026-07-27", 102.0, 51.0),    # in-sample: +2% / +2%
            mark("2026-11-09", 102.0, 51.0),    # last in-sample day (flat)
            mark("2026-11-10", 103.02, 52.02),  # forward: +1% / +2%
        ]

    def test_split_at_the_registration_mark(self, tmp_path):
        b = hedged_book(write(tmp_path / "b.jsonl", self.rows()))
        assert b["ratio"] == 0.5
        assert b["in_sample"]["n_days"] == 2
        assert b["in_sample"]["through"] == "2026-11-09"
        assert b["in_sample"]["unhedged"]["return_pct"] == pytest.approx(2.0)
        assert b["in_sample"]["hedged"]["return_pct"] == pytest.approx(1.0)
        assert "NOT evidence" in b["in_sample"]["note"]
        assert b["forward"]["n_days"] == 1
        assert b["forward"]["through"] == "2026-11-10"
        assert b["forward"]["unhedged"]["return_pct"] == pytest.approx(1.0)
        assert b["forward"]["hedged"]["return_pct"] == pytest.approx(0.0)

    def test_pre_base_history_is_ignored(self, tmp_path):
        # the 07-23 -> 07-24 jump (+11%) belongs to neither window
        b = hedged_book(write(tmp_path / "b.jsonl", self.rows()))
        assert b["in_sample"]["unhedged"]["return_pct"] < 5

    def test_no_forward_session_says_so(self, tmp_path):
        b = hedged_book(write(tmp_path / "b.jsonl", self.rows()[:-1]))
        assert b["forward"]["n_days"] == 0
        assert b["forward"]["from_mark"] == "2026-11-09"
        assert "no forward session" in b["forward"]["note"]

    def test_stale_series_is_visible_in_through(self, tmp_path):
        # a frozen benchmark file cannot look current: the date is reported
        b = hedged_book(write(tmp_path / "b.jsonl", self.rows()))
        assert b["forward"]["through"] == "2026-11-10"

    def test_corrupt_file_reports_an_error_not_a_quiet_block(self, tmp_path):
        p = tmp_path / "b.jsonl"
        p.write_text('{"date": "2026-11-10", "virtual_equity": 1\nnot json\n')
        with pytest.raises(json.JSONDecodeError):
            hedged_book(p)
        b = hedged_book_safe(p)
        assert b["status"].startswith("ERROR")
        assert "JSONDecodeError" in b["error"]
        assert "forward" not in b and "in_sample" not in b

    def test_shadow_status_carries_the_block(self, tmp_path, monkeypatch):
        import autoswing.config as cfg
        from autoswing.commands import shadow as shadow_cmd

        (tmp_path / "state").mkdir()
        write(tmp_path / "state" / "benchmark.jsonl", self.rows())
        monkeypatch.setattr(cfg, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(
            shadow_cmd, "_shadow_paths",
            lambda wide: (tmp_path / f"pos-{wide}.json",
                          tmp_path / f"ledger-{wide}.jsonl"))
        out = shadow_cmd._shadow_status()
        assert set(out) == {"v2", "wide", "hedged_book"}
        assert out["hedged_book"]["forward"]["n_days"] == 1
        # the registered verdict blocks are untouched by the new one
        assert out["v2"]["preregistered_verdict"]["verdict"] == "pending"
