"""Reconciler tests: the machine must never cancel real protection on a
lying snapshot, and must catch true orphans/naked positions.

Every scenario is a sequence of observations fed through evaluate() —
simulating exactly the broker misbehaviors we've observed live."""

from datetime import datetime, timedelta, timezone

from autoswing.reconcile import Observation, OrderObs, SymbolState, evaluate

CFG = {"min_polls": 2, "min_suspect_minutes": 90}
T0 = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)
INTENT = {"PENG": {"stop_loss": 71.0, "quantity": 96}}


def obs(positions=None, orders=None, fills=None, cash=50000.0, ts=T0):
    return Observation(
        ts=ts.isoformat(), positions=positions or {},
        orders=orders or [], fills=fills or [], cash=cash,
    )


def stop_order(sym="PENG", oid=9, qty=96):
    return OrderObs(order_id=oid, symbol=sym, action="SELL",
                    order_type="STP", quantity=qty, status="PreSubmitted")


def target_order(sym="PENG", oid=8, qty=96):
    return OrderObs(order_id=oid, symbol=sym, action="SELL",
                    order_type="LMT", quantity=qty, status="Submitted")


def entry_order(sym="XOM", oid=1, qty=60):
    return OrderObs(order_id=oid, symbol=sym, action="BUY",
                    order_type="LMT", quantity=qty, status="Submitted")


class TestConsistentStates:
    def test_position_with_bracket_is_consistent(self):
        state, decisions, notes = evaluate(
            obs(positions={"PENG": 96}, orders=[stop_order(), target_order()]),
            {}, INTENT, CFG, now=T0,
        )
        assert decisions == []
        assert state["PENG"].status == "consistent"

    def test_pending_entry_without_position_is_not_orphan(self):
        state, decisions, _ = evaluate(
            obs(orders=[entry_order()]), {}, {}, CFG, now=T0,
        )
        assert decisions == []
        assert state.get("XOM", SymbolState()).status == "consistent"

    def test_short_position_is_flagged_never_touched(self):
        state, decisions, notes = evaluate(
            obs(positions={"PENG": -96}), {}, INTENT, CFG, now=T0,
        )
        assert decisions == []
        assert any("unexpected short" in n for n in notes)


class TestLyingSnapshots:
    """The reason corroboration exists: one bad observation must never act."""

    def test_single_blank_snapshot_only_suspects(self):
        state, decisions, notes = evaluate(
            obs(orders=[stop_order(), target_order()]),  # position vanished!
            {}, INTENT, CFG, now=T0,
        )
        assert decisions == []  # no action on first sight
        assert state["PENG"].status == "suspect_orphan"

    def test_position_reappearing_clears_suspicion(self):
        s1, _, _ = evaluate(
            obs(orders=[stop_order(), target_order()]), {}, INTENT, CFG, now=T0
        )
        s2, decisions, notes = evaluate(
            obs(positions={"PENG": 96}, orders=[stop_order(), target_order()],
                ts=T0 + timedelta(hours=1)),
            s1, INTENT, CFG, now=T0 + timedelta(hours=1),
        )
        assert decisions == []
        assert s2["PENG"].status == "consistent"
        assert any("back to consistent" in n for n in notes)

    def test_persistence_without_age_does_not_confirm(self):
        # Two polls 5 minutes apart: polls satisfied, age not — stay suspect.
        s1, _, _ = evaluate(
            obs(orders=[stop_order()]), {}, INTENT, CFG, now=T0
        )
        s2, decisions, _ = evaluate(
            obs(orders=[stop_order()], ts=T0 + timedelta(minutes=5)),
            s1, INTENT, CFG, now=T0 + timedelta(minutes=5),
        )
        assert decisions == []
        assert s2["PENG"].status == "suspect_orphan"


class TestTrueOrphans:
    def test_side_door_deletion_confirms_slow_path(self):
        # Our incident: position deleted, no fill, cash unchanged, persists.
        s1, _, _ = evaluate(
            obs(orders=[stop_order(), target_order()]), {}, INTENT, CFG, now=T0
        )
        t2 = T0 + timedelta(minutes=95)
        s2, decisions, _ = evaluate(
            obs(orders=[stop_order(), target_order()], ts=t2),
            s1, INTENT, CFG, now=t2,
        )
        assert len(decisions) == 1
        d = decisions[0]
        assert d["action"] == "cancel_orphans"
        assert d["cause"] == "deleted"
        assert sorted(d["order_ids"]) == [8, 9]

    def test_manual_sale_confirms_fast_path(self):
        # Owner sold via app: fill exists -> certain the position closed,
        # persistence alone suffices (no 90-minute wait).
        s1, _, _ = evaluate(
            obs(orders=[stop_order()], fills=[{"symbol": "PENG", "side": "SLD"}]),
            {}, INTENT, CFG, now=T0,
        )
        t2 = T0 + timedelta(minutes=10)
        s2, decisions, _ = evaluate(
            obs(orders=[stop_order()],
                fills=[{"symbol": "PENG", "side": "SLD"}], ts=t2),
            s1, INTENT, CFG, now=t2,
        )
        assert len(decisions) == 1
        assert decisions[0]["cause"] == "sold"

    def test_cash_jump_confirms_without_fill_record(self):
        s1, _, _ = evaluate(
            obs(orders=[stop_order()], cash=50000.0), {}, INTENT, CFG, now=T0
        )
        t2 = T0 + timedelta(minutes=10)
        s2, decisions, _ = evaluate(
            obs(orders=[stop_order()], cash=56800.0, ts=t2),  # ~96*71 arrived
            s1, INTENT, CFG, now=t2,
        )
        assert len(decisions) == 1
        assert decisions[0]["cause"] == "sold_unrecorded"


class TestNakedPositions:
    def test_naked_long_gets_stop_replaced_after_confirmation(self):
        s1, d1, _ = evaluate(
            obs(positions={"PENG": 96}, orders=[target_order()]),  # no STP!
            {}, INTENT, CFG, now=T0,
        )
        assert d1 == []  # first sight: suspect only
        t2 = T0 + timedelta(minutes=60)
        s2, d2, _ = evaluate(
            obs(positions={"PENG": 96}, orders=[target_order()], ts=t2),
            s1, INTENT, CFG, now=t2,
        )
        assert len(d2) == 1
        assert d2[0]["action"] == "replace_stop"
        assert d2[0]["stop_price"] == 71.0

    def test_naked_without_intent_escalates_not_acts(self):
        s1, _, _ = evaluate(
            obs(positions={"MYST": 50}), {}, {}, CFG, now=T0
        )
        t2 = T0 + timedelta(minutes=60)
        s2, decisions, notes = evaluate(
            obs(positions={"MYST": 50}, ts=t2), s1, {}, CFG, now=t2,
        )
        assert decisions == []
        assert any("ESCALATE" in n for n in notes)
