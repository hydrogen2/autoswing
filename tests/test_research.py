"""Research instrument tests: counterfactual exit engine and skip scoring."""

import json
from datetime import date

import pandas as pd

from autoswing.research import (
    LiveTrade,
    compare_exit_rules,
    extract_live_trades,
    score_skips,
    simulate_exit,
    validate_skip,
)


def make_df(rows, start="2026-08-10"):
    idx = pd.bdate_range(start, periods=len(rows))
    return pd.DataFrame(
        [{"Open": o, "High": h, "Low": l, "Close": c, "Volume": 1_000_000}
         for o, h, l, c in rows], index=idx,
    )


def trade(**kw):
    base = dict(symbol="T", entry_date="2026-08-10", entry=100.0,
                stop=95.0, target=110.0, quantity=48)
    base.update(kw)
    return LiveTrade(**base)


BASELINE = {"name": "b", "target_r": 2.0, "timebox_days": 15}
TRAIL = {"name": "t", "target_r": None, "timebox_days": 15, "trail_r": 1.0}


class TestSimulateExit:
    def test_target_hit_baseline(self):
        # risk = 5, 2R target = 110
        df = make_df([(100, 104, 98, 103), (103, 111, 102, 109)])
        r = simulate_exit(trade(), df, BASELINE)
        assert r["reason"] == "target" and r["exit_price"] == 110.0
        assert r["r_multiple"] == 2.0

    def test_stop_first_on_ambiguous_bar(self):
        df = make_df([(100, 112, 94, 105)])
        r = simulate_exit(trade(), df, BASELINE)
        assert r["reason"] == "stop"

    def test_trailing_stop_locks_in_gains(self):
        # Run to 108 (trail: 108-5=103), then dip to 102 -> stopped at 103.
        df = make_df([(100, 105, 99, 104), (104, 109, 103, 108),
                      (108, 108.5, 101, 102)])
        r = simulate_exit(trade(), df, TRAIL)
        assert r["reason"] == "stop"
        assert r["exit_price"] == 103.0
        assert r["pnl"] > 0  # trailing converted a would-be giveback into profit

    def test_trailing_never_loosens(self):
        # Falling market: trail stays at original stop.
        df = make_df([(100, 101, 96, 97), (97, 98, 94, 95)])
        r = simulate_exit(trade(), df, TRAIL)
        assert r["exit_price"] == 95.0

    def test_still_open_marks_at_last_close(self):
        df = make_df([(100, 104, 98, 103)])
        r = simulate_exit(trade(), df, BASELINE, today=date(2026, 8, 10))
        assert r["reason"] == "still_open"
        assert r["pnl"] == round((103 - 100) * 48, 2)

    def test_timebox_variant_exits_earlier(self):
        rows = [(100, 104, 98, 101)] * 20
        df = make_df(rows)
        r10 = simulate_exit(trade(), df, {"name": "x", "target_r": 2.0,
                                          "timebox_days": 10})
        r15 = simulate_exit(trade(), df, BASELINE)
        assert r10["reason"] == r15["reason"] == "timebox"
        assert r10["exit_date"] < r15["exit_date"]


def _gate_approval(symbol, ts, quantity=140):
    return {
        "ts": ts, "event": "gate.decision", "dry_run": False,
        "proposal": {"symbol": symbol, "action": "BUY", "quantity": quantity,
                     "entry_limit": 34.9, "stop_loss": 32.3,
                     "take_profit": 40.3, "rationale": "PEAD: thesis"},
        "decision": {"approved": True},
    }


class TestExtractLiveTrades:
    def write_journal(self, tmp_path, events):
        p = tmp_path / "2026-08-06.jsonl"
        p.write_text("\n".join(json.dumps(e) for e in events) + "\n")
        return tmp_path

    def test_fill_evidence_gates_inclusion(self, tmp_path):
        # Regression (VOYG 2026-08-06): approved + placed, entry limit left
        # 9% behind, cancelled unfilled — yet replayed as a +2R win. An
        # approval without fill evidence must not enter the counterfactual.
        events = [
            _gate_approval("VOYG", "2026-08-06T14:10:18+00:00"),
            {"ts": "2026-08-06T14:10:20+00:00",
             "event": "broker.place_bracket_order",
             "result": {"symbol": "VOYG", "orders": [
                 {"order_id": 95, "role": "entry", "status": "Submitted"}]}},
            _gate_approval("VCTR", "2026-08-06T14:11:00+00:00", quantity=44),
            {"ts": "2026-08-06T15:46:43+00:00", "event": "broker.recent_fills",
             "result": [{"symbol": "VCTR", "side": "BOT", "shares": 44.0,
                         "price": 109.75, "time": "2026-08-06 14:11:19+00:00",
                         "order_id": 102}]},
        ]
        trades = extract_live_trades(self.write_journal(tmp_path, events))
        assert [t.symbol for t in trades] == ["VCTR"]

    def test_entry_leg_already_filled_in_place_event_counts(self, tmp_path):
        # WDFC 2026-07-10 filled instantly: the fill shows only as the entry
        # leg's status in broker.place_bracket_order, never in recent_fills.
        events = [
            _gate_approval("WDFC", "2026-08-06T14:04:35+00:00", quantity=25),
            {"ts": "2026-08-06T14:04:37+00:00",
             "event": "broker.place_bracket_order",
             "result": {"symbol": "WDFC", "orders": [
                 {"order_id": 21, "role": "entry", "status": "Filled"}]}},
        ]
        trades = extract_live_trades(self.write_journal(tmp_path, events))
        assert [t.symbol for t in trades] == ["WDFC"]

    def test_next_day_fill_does_not_validate_prior_attempt(self, tmp_path):
        # Same-day matching: a re-entry that fills tomorrow must not launder
        # today's unfilled attempt into the trade list.
        events = [
            _gate_approval("VOYG", "2026-08-06T14:10:18+00:00"),
            {"ts": "2026-08-07T15:46:43+00:00", "event": "broker.recent_fills",
             "result": [{"symbol": "VOYG", "side": "BOT", "shares": 140.0,
                         "price": 36.0, "time": "2026-08-07 14:11:19+00:00",
                         "order_id": 120}]},
        ]
        assert extract_live_trades(self.write_journal(tmp_path, events)) == []


class TestCompareRules:
    def test_all_rules_reported(self):
        df = make_df([(100, 104, 98, 103), (103, 111, 102, 109)])
        out = compare_exit_rules([trade()], {"T": df}, today=date(2026, 8, 11))
        assert len(out) == 4
        for stats in out.values():
            assert stats["trades"] == 1


class TestSkips:
    def test_validate(self):
        assert validate_skip({"symbol": "X", "category": "sold_off",
                              "reason": "market rejected it"}) == []
        assert validate_skip({"symbol": "X", "category": "vibes",
                              "reason": "r"}) != []

    def test_scoring_and_categories(self):
        rows = [(100, 102, 99, 100)] + [(100, 106, 99, 105)] * 15
        df = make_df(rows)
        skips = [{"symbol": "T", "date": df.index[0].date().isoformat(),
                  "category": "sold_off", "reason": "x"}]
        out = score_skips(skips, {"T": df}, today=date(2026, 9, 15))
        assert out["by_category"]["sold_off"]["n"] == 1
        assert out["scored"][0]["fwd_5d_pct"] == 5.0

    def test_too_recent_stays_pending(self):
        df = make_df([(100, 102, 99, 100)] * 3)
        skips = [{"symbol": "T", "date": df.index[0].date().isoformat(),
                  "category": "capacity", "reason": "x"}]
        out = score_skips(skips, {"T": df}, today=df.index[2].date())
        assert out["pending"] == 1 and out["scored"] == []


class TestStopGeometryReplay:
    """The counterfactual must never let a guessed stop reach the verdict —
    this category is DEFINED by the stop distance, so an inferred one is the
    one number we cannot fake."""

    def frame(self, closes, lows=None, highs=None, start="2026-08-03"):
        import pandas as pd
        n = len(closes)
        return pd.DataFrame({
            "Open": closes, "Close": closes,
            "High": highs or [c * 1.01 for c in closes],
            "Low": lows or [c * 0.99 for c in closes],
            "Volume": [1_000_000] * n,
        }, index=pd.bdate_range(start, periods=n))

    def test_logged_geometry_is_used_verbatim(self):
        from autoswing.research import replay_skip
        df = self.frame([100.0, 100.0, 100.0, 88.0])   # last bar low 87.12
        skip = {"symbol": "X", "date": df.index[2].date().isoformat(),
                "category": "stop_geometry", "entry": 100.0, "stop": 90.0}
        r = replay_skip(skip, df, 15, df.index[-1].date())
        assert r["basis"] == "logged"
        assert r["entry"] == 100.0 and r["stop"] == 90.0
        assert r["stop_distance_pct"] == 10.0

    def test_reconstruction_uses_the_reaction_day_low(self):
        # biggest move is the middle bar; its low must become the stop, not
        # the narrow-range bar next to it
        from autoswing.research import replay_skip
        df = self.frame([100.0, 112.0, 112.5],
                        lows=[99.0, 104.0, 111.8], highs=[101.0, 113.0, 113.0])
        skip = {"symbol": "X", "date": df.index[2].date().isoformat(),
                "category": "stop_geometry"}
        r = replay_skip(skip, df, 15, df.index[-1].date())
        assert r["stop"] == 104.0          # reaction-day low, not 111.8
        assert r["basis"] == "reconstructed"

    def test_implausibly_tight_reconstruction_is_marked_unreliable(self):
        from autoswing.research import replay_skip
        df = self.frame([100.0, 100.2, 100.1],
                        lows=[99.9, 100.0, 100.0], highs=[100.3, 100.4, 100.2])
        skip = {"symbol": "X", "date": df.index[2].date().isoformat(),
                "category": "stop_geometry"}
        assert replay_skip(skip, df, 15, df.index[-1].date())["basis"] == \
            "unreliable_reconstruction"

    def test_verdict_counts_logged_only_and_gates_on_n(self):
        from autoswing.research import replay_stop_geometry_skips
        df = self.frame([100.0, 100.0, 100.0, 88.0])   # last bar low 87.12
        d = df.index[2].date().isoformat()
        skips = [
            {"symbol": "L", "date": d, "category": "stop_geometry",
             "entry": 100.0, "stop": 90.0},
            {"symbol": "R", "date": d, "category": "stop_geometry"},
            {"symbol": "OTHER", "date": d, "category": "liquidity",
             "entry": 100.0, "stop": 90.0},
        ]
        hist = {"L": df, "R": df, "OTHER": df}
        out = replay_stop_geometry_skips(skips, hist, 15, df.index[-1].date())
        assert out["n_replayed"] == 2                    # liquidity excluded
        assert out["verdict"]["n"] == 1                  # logged only
        assert out["verdict"]["verdict_ready"] is False  # n=1 < 20
        assert out["verdict"]["basis"] == "logged geometry only"
        assert "caveat" in out["indicative_reconstructed"]

    def test_skip_validation_requires_entry_and_stop_together(self):
        from autoswing.research import validate_skip
        base = {"symbol": "X", "category": "stop_geometry", "reason": "wide stop"}
        assert validate_skip(base) == []
        assert validate_skip({**base, "entry": 100.0})
        assert validate_skip({**base, "stop": 90.0})
        assert validate_skip({**base, "entry": 100.0, "stop": 90.0}) == []
        assert validate_skip({**base, "entry": 90.0, "stop": 100.0})
