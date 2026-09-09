"""Shadow book tests: the virtual execution engine must be conservative
and deterministic."""

from datetime import date

import pandas as pd
import pytest

from autoswing.shadow import ShadowPosition, ledger_stats, mark_position


def make_df(rows, start="2026-08-10"):
    """rows: list of (open, high, low, close)"""
    idx = pd.bdate_range(start, periods=len(rows))
    return pd.DataFrame(
        [{"Open": o, "High": h, "Low": l, "Close": c, "Volume": 1_000_000}
         for o, h, l, c in rows], index=idx,
    )


def pos(**overrides):
    base = dict(
        symbol="TEST", strategy="news-v2", opened="2026-08-10",
        entry_price=100.0, quantity=48, stop_loss=95.0, take_profit=112.0,
    )
    base.update(overrides)
    return ShadowPosition(**base)


class TestMarking:
    def test_stays_open_when_nothing_hit(self):
        df = make_df([(100, 104, 98, 101), (101, 105, 99, 103)])
        assert mark_position(pos(), df, date(2026, 8, 11), 15) is None

    def test_stop_hit(self):
        df = make_df([(100, 104, 98, 101), (100, 102, 94, 96)])
        e = mark_position(pos(), df, date(2026, 8, 11), 15)
        assert e["reason"] == "stop"
        assert e["exit_price"] == 95.0
        assert e["pnl"] == pytest.approx((95 - 100) * 48)

    def test_target_hit(self):
        df = make_df([(100, 104, 98, 101), (100, 113, 99, 111)])
        e = mark_position(pos(), df, date(2026, 8, 11), 15)
        assert e["reason"] == "target"
        assert e["pnl"] == pytest.approx((112 - 100) * 48)

    def test_ambiguous_bar_resolves_stop_first(self):
        # Low breaches stop AND high reaches target: conservatism wins.
        df = make_df([(100, 104, 98, 101), (100, 115, 94, 108)])
        e = mark_position(pos(), df, date(2026, 8, 11), 15)
        assert e["reason"] == "stop"

    def test_timebox_closes_at_close(self):
        rows = [(100, 104, 98, 101)] * 16
        df = make_df(rows)
        e = mark_position(pos(), df, date(2026, 9, 5), 15)
        assert e["reason"] == "timebox"
        assert e["exit_price"] == 101.0

    def test_entry_day_pre_entry_low_does_not_stop(self):
        # 2026-09-09 wide-ASO regression: the entry-day low printed before
        # the mid-session entry existed; the close back inside the bracket
        # proves nothing post-entry — the position must stay open.
        df = make_df([(100, 102, 94, 96)])
        assert mark_position(pos(), df, date(2026, 8, 10), 15) is None

    def test_entry_day_touch_of_target_does_not_fill(self):
        # Same ambiguity on the upside: an intraday target touch with a
        # close inside the bracket holds until the next session.
        df = make_df([(100, 113, 99, 111)])
        assert mark_position(pos(), df, date(2026, 8, 10), 15) is None

    def test_entry_day_close_through_stop_fills(self):
        # Entry is above the stop, so a day-0 close at/through the stop
        # proves the stop was crossed after entry.
        df = make_df([(100, 102, 93, 94)])
        e = mark_position(pos(), df, date(2026, 8, 10), 15)
        assert e["reason"] == "stop"
        assert e["exit_price"] == 95.0

    def test_entry_day_close_through_target_fills(self):
        df = make_df([(100, 115, 99, 113)])
        e = mark_position(pos(), df, date(2026, 8, 10), 15)
        assert e["reason"] == "target"
        assert e["exit_price"] == 112.0

    def test_entry_day_close_through_both_resolves_stop_first(self):
        # Degenerate close-at-stop with a target touch earlier in the bar:
        # stop-first conservatism applies on day 0 too.
        df = make_df([(100, 115, 94, 95)])
        e = mark_position(pos(), df, date(2026, 8, 10), 15)
        assert e["reason"] == "stop"

    def test_next_session_full_bar_logic_resumes(self):
        # Day 0 ambiguous touch holds; day 1 low touch stops normally.
        df = make_df([(100, 102, 94, 96), (96, 98, 94.5, 97)])
        e = mark_position(pos(), df, date(2026, 8, 11), 15)
        assert e["reason"] == "stop"
        assert e["closed"] == "2026-08-11"

    def test_bars_before_open_ignored(self):
        # A pre-entry crash bar must not close the position.
        df = make_df([(80, 85, 70, 80), (100, 104, 98, 102)])
        p = pos(opened=df.index[1].date().isoformat())
        assert mark_position(p, df, df.index[1].date(), 15) is None

    def test_bars_after_today_ignored(self):
        df = make_df([(100, 104, 98, 101), (96, 97, 90, 92)])
        # today = first bar only; the future stop-day must not count yet.
        assert mark_position(pos(), df, df.index[0].date(), 15) is None


class TestLedgerStats:
    def test_stats(self, tmp_path):
        p = tmp_path / "ledger.jsonl"
        p.write_text('{"pnl": 100.0, "alpha_pct": 3.0}\n{"pnl": -40.0, "alpha_pct": -1.0}\n'
                     '{"pnl": 25.5}\n')  # third close predates alpha stamping
        s = ledger_stats(p)
        assert s == {"closed": 3, "wins": 2, "losses": 1, "total_pnl": 85.5,
                     "avg_alpha_pct": 1.0, "alpha_n": 2}

    def test_stats_no_alpha_rows(self, tmp_path):
        p = tmp_path / "ledger.jsonl"
        p.write_text('{"pnl": 10.0}\n')
        s = ledger_stats(p)
        assert s["avg_alpha_pct"] is None and s["alpha_n"] == 0

    def test_empty(self, tmp_path):
        s = ledger_stats(tmp_path / "none.jsonl")
        assert s["closed"] == 0
