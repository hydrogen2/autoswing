"""Regression: benchmark-mark must be idempotent per date.

On 2026-08-03 the premarket brain run mistakenly executed `benchmark-mark`
(a preclose-only task), appending a stale early row for the day. The
authoritative preclose mark then appended a SECOND 2026-08-03 row, because
`_benchmark_mark` opened the series file in append mode with no dedup. Two
rows for one date double-count in any per-day aggregation of the scoreboard.
The mark must replace an existing same-date row (last-write-wins), and the
merge must self-heal a series that already carries same-date duplicates.
"""

import pandas as pd

from autoswing.commands.trading import _merge_benchmark_entry, _regime_tags


def _dates(series):
    return [row["date"] for row in series]


class TestMergeBenchmarkEntry:
    def test_same_day_rerun_replaces_not_appends(self):
        existing = [
            {"date": "2026-07-09", "virtual_equity": 50000.0},
            {"date": "2026-08-03", "virtual_equity": 48121.59},  # errant premarket
        ]
        entry = {"date": "2026-08-03", "virtual_equity": 48957.16}  # preclose
        series = _merge_benchmark_entry(existing, entry)
        assert _dates(series) == ["2026-07-09", "2026-08-03"]
        assert series[-1]["virtual_equity"] == 48957.16

    def test_new_day_appends_in_order(self):
        existing = [{"date": "2026-07-31", "virtual_equity": 48195.77}]
        entry = {"date": "2026-08-03", "virtual_equity": 48957.16}
        series = _merge_benchmark_entry(existing, entry)
        assert _dates(series) == ["2026-07-31", "2026-08-03"]

    def test_self_heals_preexisting_duplicate(self):
        # Series already polluted with two rows for the same date.
        existing = [
            {"date": "2026-07-09", "virtual_equity": 50000.0},
            {"date": "2026-08-03", "virtual_equity": 48121.59},
            {"date": "2026-08-03", "virtual_equity": 48957.16},
        ]
        entry = {"date": "2026-08-04", "virtual_equity": 49010.0}
        series = _merge_benchmark_entry(existing, entry)
        assert _dates(series) == ["2026-07-09", "2026-08-03", "2026-08-04"]
        # The surviving 08-03 row is the last one seen (authoritative).
        assert series[1]["virtual_equity"] == 48957.16

    def test_first_row_stays_inception_anchor(self):
        existing = [{"date": "2026-07-09", "virtual_equity": 50000.0}]
        entry = {"date": "2026-07-09", "virtual_equity": 50000.0}
        series = _merge_benchmark_entry(existing, entry)
        assert len(series) == 1
        assert series[0]["date"] == "2026-07-09"


def _frame(closes):
    return pd.DataFrame({"Close": closes})


class TestRegimeTags:
    """Regression for 2026-09-08: the SPY download failed (yfinance
    "possibly delisted" warning) and the day's benchmark row silently lost
    spy_vs_20dma_pct — a fetch failure rendering as a benign thinner row.
    A missing input must come back as a NAMED gap."""

    def test_all_inputs_present(self):
        hist = {"SPY": _frame([100.0] * 25), "^VIX": _frame([15.5])}
        regime, gaps = _regime_tags(hist)
        assert regime == {"spy_vs_20dma_pct": 0.0, "vix_close": 15.5}
        assert gaps == []

    def test_missing_spy_is_a_named_gap(self):
        hist = {"^VIX": _frame([15.5])}
        regime, gaps = _regime_tags(hist)
        assert "spy_vs_20dma_pct" not in regime
        assert gaps == ["spy_vs_20dma_pct"]

    def test_short_spy_history_is_a_named_gap(self):
        # 19 rows can't support a 20-day mean; that's a gap, not a tag.
        hist = {"SPY": _frame([100.0] * 19), "^VIX": _frame([15.5])}
        _, gaps = _regime_tags(hist)
        assert gaps == ["spy_vs_20dma_pct"]

    def test_missing_vix_is_a_named_gap(self):
        hist = {"SPY": _frame([100.0] * 25)}
        _, gaps = _regime_tags(hist)
        assert gaps == ["vix_close"]

    def test_both_missing(self):
        _, gaps = _regime_tags({})
        assert gaps == ["spy_vs_20dma_pct", "vix_close"]
