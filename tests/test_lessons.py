"""Reflection memory: closed-trade extraction must see both close paths,
exclude unfilled entries, compute honest outcomes, and keep lessons terse."""

import json
from pathlib import Path

import pytest

from autoswing.lessons import (
    ClosedTrade,
    extract_closed_trades,
    lessons_context,
    outcome,
    validate_lesson,
)


def write_journal(tmp_path: Path, day: str, events: list[dict]) -> None:
    (tmp_path / "journal").mkdir(exist_ok=True)
    with open(tmp_path / "journal" / f"{day}.jsonl", "a") as f:
        for i, e in enumerate(events):
            f.write(json.dumps({"ts": f"{day}T14:0{i}:00+00:00", **e}) + "\n")


def approval(sym, entry, stop, target, rationale="thesis text here"):
    return {"event": "gate.decision", "dry_run": False,
            "decision": {"approved": True},
            "proposal": {"symbol": sym, "entry_limit": entry, "stop_loss": stop,
                         "take_profit": target, "rationale": rationale,
                         "strategy": "pead-v1"}}


def buy_fill(sym, day, price):
    return {"event": "broker.recent_fills",
            "result": [{"symbol": sym, "side": "BOT", "price": price,
                        "time": f"{day} 14:01:00"}]}


def sell_fill(sym, day, price):
    return {"event": "broker.recent_fills",
            "result": [{"symbol": sym, "side": "SLD", "price": price,
                        "time": f"{day} 15:00:00"}]}


def position_closed(sym, placed, entry, stop, target):
    return {"event": "manage.position_closed", "symbol": sym,
            "meta": {"symbol": sym, "placed_date": placed, "entry_limit": entry,
                     "stop_loss": stop, "take_profit": target,
                     "rationale": "thesis text here", "strategy": "pead-v1"}}


def enforced_review(sym, action="exit_timebox"):
    return {"event": "manage.review",
            "result": {"positions": [{"symbol": sym, "action": action,
                                      "enforced": True}]}}


class TestExtraction:
    def test_bracket_stop_close(self, tmp_path):
        write_journal(tmp_path, "2026-08-01",
                      [approval("XOM", 100, 95, 110), buy_fill("XOM", "2026-08-01", 100)])
        write_journal(tmp_path, "2026-08-04",
                      [sell_fill("XOM", "2026-08-04", 94.9),
                       position_closed("XOM", "2026-08-01", 100, 95, 110)])
        [t] = extract_closed_trades(tmp_path / "journal")
        assert t.id == "XOM-2026-08-01"
        assert t.closed_date == "2026-08-04"
        assert t.exit_price == 94.9
        assert t.exit_kind == "stop"

    def test_target_close_inferred(self, tmp_path):
        write_journal(tmp_path, "2026-08-01",
                      [approval("XOM", 100, 95, 110), buy_fill("XOM", "2026-08-01", 100)])
        write_journal(tmp_path, "2026-08-06",
                      [sell_fill("XOM", "2026-08-06", 110.2),
                       position_closed("XOM", "2026-08-01", 100, 95, 110)])
        [t] = extract_closed_trades(tmp_path / "journal")
        assert t.exit_kind == "target"

    def test_enforced_timebox_close_is_captured(self, tmp_path):
        # Time-box exits delete meta in-process and never emit
        # position_closed; they must still be reflectable (they're the
        # profitable exits — missing them biases lessons toward stop-outs).
        write_journal(tmp_path, "2026-07-20",
                      [approval("TRV", 369, 340, 427), buy_fill("TRV", "2026-07-20", 368.6)])
        write_journal(tmp_path, "2026-08-10",
                      [sell_fill("TRV", "2026-08-10", 375.5), enforced_review("TRV")])
        [t] = extract_closed_trades(tmp_path / "journal")
        assert t.id == "TRV-2026-07-20"
        assert t.exit_kind == "timebox"
        assert t.exit_price == 375.5
        assert t.rationale == "thesis text here"

    def test_unfilled_entry_is_not_a_trade(self, tmp_path):
        # VOYG 08-06: approved, placed, ran away unfilled, cancelled — the
        # meta got deleted (position_closed) but nothing was ever owned.
        write_journal(tmp_path, "2026-08-06",
                      [approval("VOYG", 34.9, 32.3, 40.3),
                       position_closed("VOYG", "2026-08-06", 34.9, 32.3, 40.3)])
        assert extract_closed_trades(tmp_path / "journal") == []

    def test_adopted_and_healthcheck_excluded(self, tmp_path):
        write_journal(tmp_path, "2026-08-01", [
            approval("XOM", 100, 95, 110, rationale="healthcheck"),
            buy_fill("XOM", "2026-08-01", 100),
            {"event": "manage.position_closed", "symbol": "ZZZ",
             "meta": {"symbol": "ZZZ", "placed_date": "2026-08-01",
                      "entry_limit": 10, "stop_loss": 0.0, "take_profit": 0.0,
                      "rationale": "adopted: position existed without metadata"}},
        ])
        write_journal(tmp_path, "2026-08-02", [enforced_review("XOM")])
        # XOM healthcheck approval never becomes a trade; ZZZ adopted is skipped
        assert extract_closed_trades(tmp_path / "journal") == []

    def test_no_sell_evidence_leaves_exit_none(self, tmp_path):
        write_journal(tmp_path, "2026-08-01",
                      [approval("XOM", 100, 95, 110), buy_fill("XOM", "2026-08-01", 100)])
        write_journal(tmp_path, "2026-08-04",
                      [position_closed("XOM", "2026-08-01", 100, 95, 110)])
        [t] = extract_closed_trades(tmp_path / "journal")
        assert t.exit_price is None and t.exit_kind == "unknown"


class TestOutcome:
    def trade(self, exit_price):
        return ClosedTrade(symbol="X", placed_date="2026-08-01",
                           closed_date="2026-08-05", entry_limit=100.0,
                           stop_loss=95.0, take_profit=110.0, rationale="",
                           strategy="pead-v1", exit_price=exit_price)

    def test_r_and_alpha(self):
        o = outcome(self.trade(110.0), bench_entry=500.0, bench_exit=510.0)
        assert o["r_multiple"] == 2.0
        assert o["return_pct"] == 10.0
        assert o["alpha_pct"] == pytest.approx(8.0)   # 10% - 2% benchmark

    def test_missing_legs_are_none_not_guessed(self):
        o = outcome(self.trade(None), 500.0, 510.0)
        assert o["r_multiple"] is None and o["alpha_pct"] is None
        o = outcome(self.trade(105.0), None, None)
        assert o["return_pct"] == 5.0 and o["alpha_pct"] is None


class TestLessonValidation:
    def good(self, **kw):
        base = {"symbol": "XOM", "closed_date": "2026-08-05", "thesis_held": "failed",
                "lesson": "The beat was clean but the sector sold off; the stop at "
                          "the reaction low was correct and cheap. Next time weight "
                          "sector tape more when the print is in-line with peers."}
        base.update(kw)
        return base

    def test_valid(self):
        assert validate_lesson(self.good()) == []

    def test_thesis_enum(self):
        assert validate_lesson(self.good(thesis_held="kinda"))

    def test_too_long_rejected(self):
        assert any("600" in e for e in validate_lesson(self.good(lesson="x " * 400)))

    def test_too_short_rejected(self):
        assert validate_lesson(self.good(lesson="it stopped out"))


class TestContext:
    def lessons(self):
        return [
            {"symbol": "XOM", "placed_date": "2026-08-01", "closed_date": "2026-08-05",
             "exit_kind": "stop", "r_multiple": -1.0, "alpha_pct": -3.2,
             "thesis_held": "failed", "lesson": "xom lesson one"},
            {"symbol": "CVX", "placed_date": "2026-08-02", "closed_date": "2026-08-09",
             "exit_kind": "timebox", "r_multiple": 0.7, "alpha_pct": None,
             "thesis_held": "held", "lesson": "cvx lesson"},
            {"symbol": "XOM", "placed_date": "2026-07-01", "closed_date": "2026-07-10",
             "exit_kind": "target", "r_multiple": 2.0, "alpha_pct": 5.0,
             "thesis_held": "held", "lesson": "xom lesson old"},
        ]

    def test_same_symbol_first_most_recent(self):
        ctx = lessons_context(self.lessons(), "XOM")
        assert ctx.index("xom lesson one") < ctx.index("xom lesson old") < ctx.index("cvx lesson")
        assert "-1.0R" in ctx and "alpha -3.2%" in ctx and "R n/a" not in ctx.split("cvx")[0]

    def test_no_symbol_gives_recent_cross(self):
        ctx = lessons_context(self.lessons(), None)
        assert "Recent lessons" in ctx and "Past" not in ctx

    def test_empty(self):
        assert lessons_context([], "XOM") == ""

    def test_bounded(self):
        many = [dict(self.lessons()[1], closed_date=f"2026-07-{d:02d}", lesson=f"l{d}")
                for d in range(1, 29)]
        ctx = lessons_context(many, "XOM", n_cross=6)
        assert ctx.count("\n- ") == 6
