"""Tests for the parts that must never be wrong: the paper/live interlock
and bracket-order validation."""

import json

import pytest

from autoswing.broker import BracketProposal, _is_entry_order, _validate_bracket
from autoswing.config import (
    LIVE_PORT,
    PAPER_PORT,
    BrokerConfig,
    LiveTradingRefused,
    enforce_paper_interlock,
)
from autoswing.journal import Journal


def _broker_cfg(port=PAPER_PORT, live_trading=False):
    return BrokerConfig(
        host="127.0.0.1", port=port, client_id=1,
        live_trading=live_trading, connect_timeout_s=15,
    )


class TestPaperLiveInterlock:
    def test_paper_default_passes(self, monkeypatch):
        monkeypatch.delenv("AUTOSWING_LIVE", raising=False)
        enforce_paper_interlock(_broker_cfg())  # must not raise

    def test_live_port_alone_refused(self, monkeypatch):
        monkeypatch.delenv("AUTOSWING_LIVE", raising=False)
        with pytest.raises(LiveTradingRefused):
            enforce_paper_interlock(_broker_cfg(port=LIVE_PORT))

    def test_live_flag_alone_refused(self, monkeypatch):
        monkeypatch.delenv("AUTOSWING_LIVE", raising=False)
        with pytest.raises(LiveTradingRefused):
            enforce_paper_interlock(_broker_cfg(live_trading=True))

    def test_env_alone_refused(self, monkeypatch):
        monkeypatch.setenv("AUTOSWING_LIVE", "1")
        with pytest.raises(LiveTradingRefused):
            enforce_paper_interlock(_broker_cfg())

    def test_flag_and_env_but_paper_port_refused(self, monkeypatch):
        # Even with both opt-ins, pointing at the paper port is ambiguous.
        monkeypatch.setenv("AUTOSWING_LIVE", "1")
        with pytest.raises(LiveTradingRefused):
            enforce_paper_interlock(_broker_cfg(live_trading=True))

    def test_all_three_interlocks_pass(self, monkeypatch):
        monkeypatch.setenv("AUTOSWING_LIVE", "1")
        enforce_paper_interlock(_broker_cfg(port=LIVE_PORT, live_trading=True))


def _bracket(**overrides):
    base = dict(
        symbol="AAPL", action="BUY", quantity=10,
        entry_limit=100.0, stop_loss=95.0, take_profit=110.0,
    )
    base.update(overrides)
    return BracketProposal(**base)


class TestBracketValidation:
    def test_valid_buy_passes(self):
        _validate_bracket(_bracket())

    def test_valid_sell_passes(self):
        _validate_bracket(
            _bracket(action="SELL", stop_loss=105.0, take_profit=90.0)
        )

    def test_zero_quantity_rejected(self):
        with pytest.raises(ValueError):
            _validate_bracket(_bracket(quantity=0))

    def test_negative_quantity_rejected(self):
        with pytest.raises(ValueError):
            _validate_bracket(_bracket(quantity=-5))

    def test_buy_with_stop_above_entry_rejected(self):
        with pytest.raises(ValueError):
            _validate_bracket(_bracket(stop_loss=101.0))

    def test_buy_with_target_below_entry_rejected(self):
        with pytest.raises(ValueError):
            _validate_bracket(_bracket(take_profit=99.0))

    def test_sell_with_inverted_prices_rejected(self):
        with pytest.raises(ValueError):
            _validate_bracket(
                _bracket(action="SELL", stop_loss=95.0, take_profit=110.0)
            )

    def test_nonsense_action_rejected(self):
        with pytest.raises(ValueError):
            _validate_bracket(_bracket(action="YOLO"))

    def test_negative_price_rejected(self):
        with pytest.raises(ValueError):
            _validate_bracket(_bracket(stop_loss=-1.0))


class TestEntryOrderClassification:
    """Regression: 2026-07-23 — every entry was blocked all day because the
    four live take-profit legs were counted as new exposure, putting gross at
    $55,591 against a $47,974 cap while the positions themselves cost $26,041.
    IB had reported those bracket children with parentId==0 after reconnect.
    """

    def test_reconnected_bracket_child_is_not_an_entry(self):
        # parentId lost on reconnect, but it offsets a long we hold.
        assert not _is_entry_order("SELL", 0, held_quantity=41.0)

    def test_bracket_child_with_parent_is_not_an_entry(self):
        assert not _is_entry_order("SELL", 47, held_quantity=41.0)

    def test_fresh_buy_with_no_position_is_an_entry(self):
        assert _is_entry_order("BUY", 0, held_quantity=0.0)

    def test_buy_adding_to_existing_long_is_an_entry(self):
        assert _is_entry_order("BUY", 0, held_quantity=41.0)

    def test_short_entry_is_an_entry(self):
        assert _is_entry_order("SELL", 0, held_quantity=0.0)

    def test_buy_to_cover_a_short_is_not_an_entry(self):
        assert not _is_entry_order("BUY", 0, held_quantity=-41.0)

    def test_gross_exposure_counts_positions_once(self):
        """The exact 2026-07-23 book: exposure must be cost, not cost+targets."""
        positions = {"JBHT": 22.0, "TRV": 16.0, "MMM": 41.0, "ABT": 64.0}
        # (symbol, action, parentId, quantity, lmtPrice) as IB replayed them.
        working = [
            ("MMM", "SELL", 0, 41.0, 188.5), ("ABT", "SELL", 0, 64.0, 117.0),
            ("JBHT", "SELL", 0, 22.0, 341.0), ("TRV", "SELL", 0, 16.0, 427.0),
        ]
        counted = sum(
            qty * px
            for sym, action, parent, qty, px in working
            if _is_entry_order(action, parent, positions.get(sym, 0.0))
        )
        assert counted == 0.0


class TestJournal:
    def test_append_only_jsonl(self, tmp_path):
        j = Journal(tmp_path)
        j.record("test.event", foo=1)
        j.record("test.event", foo=2)
        files = list(tmp_path.glob("*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text().strip().splitlines()
        assert len(lines) == 2
        entries = [json.loads(l) for l in lines]
        assert entries[0]["foo"] == 1
        assert entries[1]["foo"] == 2
        assert all("ts" in e and "event" in e for e in entries)
