"""Pre-registered wide-ledger second period: extras against taken trades
(owner kept the ledger 2026-11-12; registered the same day).

Same anti-renegotiation intent as the other registrations: pin the numbers,
pin the sample boundary on both sides, and pin the classification -- the
part that went wrong when it was done by eye. In the first 40 closes the
nine "wide-only" rows read +2.5% alpha; three of them were not capacity
extras at all (two live orders that never filled, one name taken a day
later) and the six real ones all lost money.
"""

import json

import pytest

from autoswing import wide_extras as wx
from autoswing.wide_extras import (
    classify_wide_row, wide_entry_evidence, wide_extras_verdict,
    wide_extras_verdict_safe,
)

D0, D1 = "2026-11-16", "2026-11-20"


def row(sym, pnl, alpha, opened=D0, closed=D1):
    qty, entry = 50, 100.0
    return {"symbol": sym, "opened": opened, "closed": closed,
            "entry_price": entry, "exit_price": round(entry + pnl / qty, 4),
            "quantity": qty, "pnl": pnl, "alpha_pct": alpha}


class Book:
    """Ledger rows plus the journal evidence that classes them."""

    def __init__(self):
        self.rows = []
        self.ev = {"approved": set(), "filled": set(), "logged": set()}
        self._i = 0

    def add(self, kind, pnl, alpha, opened=D0, closed=D1, sym=None):
        self._i += 1
        sym = sym or f"{kind[:2].upper()}{self._i}"
        r = row(sym, pnl, alpha, opened, closed)
        self.rows.append(r)
        if kind != "unclassified":
            self.ev["logged"].add((sym, opened))
        if kind in ("taken", "unfilled"):
            self.ev["approved"].add((sym, opened))
        if kind == "taken":
            self.ev["filled"].add((sym, opened))
        if kind == "taken_later":
            self.ev["approved"].add((sym, closed))
            self.ev["filled"].add((sym, closed))
        return r

    def many(self, kind, n, pnl, alpha):
        for _ in range(n):
            self.add(kind, pnl, alpha)
        return self

    def verdict(self):
        return wide_extras_verdict(self.rows, self.ev)


# --- the registration itself --------------------------------------------------

def test_registration_is_pinned():
    assert wx.WIDE_EXTRAS_REGISTERED == "2026-11-12"
    assert wx.WIDE_EXTRAS_SAMPLE_FROM == "2026-11-12"
    assert wx.WIDE_EXTRAS_N == 20
    assert wx.WIDE_EXTRAS_MIN_TAKEN == 20
    assert wx.WIDE_EXTRAS_MARGIN_PP == 2.0
    assert wx.WIDE_EXTRAS_MIN_ALPHA_COVERAGE == 0.8


def test_rule_text_states_the_pinned_numbers():
    rule = wx.WIDE_EXTRAS_RULE
    for needle in ("2026-11-12", "20th extra", "min 20", ">= 2.0pp",
                   "unfilled", "taken_later", "best trade"):
        assert needle in rule


# --- classification -----------------------------------------------------------

def ev(approved=(), filled=(), logged=(("ABC", D0),)):
    return {"approved": set(approved), "filled": set(filled),
            "logged": set(logged)}


R = row("ABC", 100, 1.0)


def test_taken_needs_approval_and_fill_on_the_opening_day():
    assert classify_wide_row(R, ev([("ABC", D0)], [("ABC", D0)])) == "taken"


def test_no_live_approval_in_the_hold_is_an_extra():
    assert classify_wide_row(R, ev()) == "extra"


def test_approved_but_never_filled_is_unfilled_not_an_extra():
    # DELL 09-03 / CBRL 09-24: the limit never filled, the stock ran +15%.
    assert classify_wide_row(R, ev([("ABC", D0)])) == "unfilled"


def test_live_entry_on_a_later_day_of_the_hold_is_taken_later():
    # HTHT: wide-logged 08-17 on a capacity reject, entered live 08-18.
    later = "2026-11-17"
    assert classify_wide_row(
        R, ev([("ABC", later)], [("ABC", later)])) == "taken_later"


def test_later_approval_that_never_fills_is_unfilled():
    assert classify_wide_row(R, ev([("ABC", "2026-11-18")])) == "unfilled"


def test_an_approval_and_a_fill_on_different_days_are_not_one_entry():
    # entry orders are DAY orders: a later approval that never filled, plus
    # an unrelated buy on the opening day, is still an unfilled entry
    later = "2026-11-18"
    assert classify_wide_row(
        R, ev([("ABC", later)], [("ABC", D0)])) == "unfilled"
    assert classify_wide_row(
        R, ev([("ABC", D0)], [("ABC", later)])) == "unfilled"


def test_fill_without_an_approved_entry_does_not_make_it_taken():
    # a wheel assignment in the same name is a BOT with no pead-v1 approval
    assert classify_wide_row(R, ev(filled=[("ABC", D0)])) == "extra"


def test_evidence_outside_the_hold_is_ignored():
    before, after = "2026-11-13", "2026-11-23"
    e = ev([("ABC", before), ("ABC", after)], [("ABC", before), ("ABC", after)])
    assert classify_wide_row(R, e) == "extra"


def test_another_symbols_entry_is_not_evidence():
    assert classify_wide_row(R, ev([("XYZ", D0)], [("XYZ", D0)])) == "extra"


def test_no_journal_record_of_the_wide_entry_is_unclassified_never_extra():
    """The benign-state trap: a lost journal day leaves no approval either,
    which must not read as 'capacity kept it out'."""
    assert classify_wide_row(R, ev(logged=())) == "unclassified"
    assert classify_wide_row(
        R, ev([("ABC", D0)], [("ABC", D0)], logged=())) == "unclassified"


# --- journal evidence ---------------------------------------------------------

def jl(ts, event, **kw):
    return json.dumps({"ts": ts, "event": event, **kw})


def gate(ts, sym, approved=True, dry=False, **prop):
    return jl(ts, "gate.decision", dry_run=dry,
              proposal={"symbol": sym, **prop},
              decision={"approved": approved, "rules": []})


def journal(tmp_path, **days):
    for day, lines in days.items():
        (tmp_path / f"{day.replace('_', '-')}.jsonl").write_text(
            "\n".join(lines) + "\n")
    return tmp_path


def test_scan_reads_approvals_fills_and_wide_entries(tmp_path):
    t = "2026-11-16T15:09:00+00:00"
    j = journal(tmp_path, **{"2026_11_16": [
        gate(t, "aaa"),
        jl(t, "broker.place_bracket_order", result={
            "symbol": "AAA", "orders": [{"role": "entry", "status": "Filled"}]}),
        gate(t, "BBB", strategy="pead-v1"),
        jl(t, "broker.place_bracket_order", result={
            "symbol": "BBB", "orders": [{"role": "entry", "status": "Submitted"}]}),
        jl(t, "broker.recent_fills", result=[
            {"symbol": "CCC", "side": "BOT", "time": "2026-11-16 15:10:01"},
            {"symbol": "DDD", "side": "SLD", "time": "2026-11-16 15:10:02"}]),
        jl(t, "shadow.proposal", proposal={"symbol": "EEE"},
           result={"wide": True, "opened_virtual": True}),
    ]})
    e = wide_entry_evidence(j)
    assert e["approved"] == {("AAA", "2026-11-16"), ("BBB", "2026-11-16")}
    assert e["filled"] == {("AAA", "2026-11-16"), ("CCC", "2026-11-16")}
    assert e["logged"] == {("EEE", "2026-11-16")}


def test_scan_ignores_everything_that_is_not_a_live_pead_entry(tmp_path):
    t = "2026-11-16T15:09:00+00:00"
    j = journal(tmp_path, **{"2026_11_16": [
        gate(t, "DRY", dry=True),
        gate(t, "REJ", approved=False),
        gate(t, "HC", rationale="healthcheck"),
        gate(t, "SZ", rationale="sizing sanity check"),
        gate(t, "V2", strategy="news-v2"),
        jl(t, "shadow.proposal", proposal={"symbol": "V2B"},
           result={"wide": False, "opened_virtual": True}),
        jl(t, "shadow.proposal", proposal={"symbol": "DUP"},
           result={"wide": True, "opened_virtual": False}),
    ]})
    e = wide_entry_evidence(j)
    assert e == {"approved": set(), "filled": set(), "logged": set()}


def test_scan_dates_a_fill_by_its_own_time_not_the_day_it_was_read(tmp_path):
    j = journal(tmp_path, **{"2026_11_17": [
        jl("2026-11-17T10:45:00+00:00", "broker.recent_fills", result=[
            {"symbol": "AAA", "side": "BOT", "time": "2026-11-16 19:59:58"}]),
    ]})
    assert wide_entry_evidence(j)["filled"] == {("AAA", "2026-11-16")}


def test_scan_starts_at_the_registration_date(tmp_path):
    j = journal(tmp_path, **{
        "2026_11_11": [gate("2026-11-11T15:09:00+00:00", "OLD")],
        "2026_11_12": [gate("2026-11-12T15:09:00+00:00", "NEW")],
    })
    assert wide_entry_evidence(j)["approved"] == {("NEW", "2026-11-12")}


def test_a_malformed_journal_line_raises_rather_than_hiding_an_approval(tmp_path):
    j = journal(tmp_path, **{"2026_11_16": ['{"event": "gate.decision", "ts'] })
    with pytest.raises(json.JSONDecodeError):
        wide_entry_evidence(j)


# --- the sample ---------------------------------------------------------------

def test_period_opens_empty_and_pending():
    v = wide_extras_verdict([], ev(logged=()))
    assert v["verdict"] == "pending"
    assert v["remaining"] == 20
    assert v["extras"]["n"] == v["taken"]["n"] == 0
    assert "not a result" in v["note"]


def test_positions_opened_before_registration_never_count():
    """The first 40 closes and the nine positions open at registration: even
    with perfect evidence and spectacular outcomes they stay out."""
    b = Book()
    for _ in range(30):
        b.add("extra", 5000, 40.0, opened="2026-11-11", closed="2026-11-13")
    for _ in range(30):
        b.add("taken", -500, -9.0, opened="2026-10-30", closed="2026-11-13")
    v = b.verdict()
    assert v["verdict"] == "pending"
    assert v["period_closed"] == 0
    assert v["extras"]["n"] == v["taken"]["n"] == 0
    assert v["excluded"]["opened_before_registration"] == 60


def test_the_registration_day_itself_is_inside_the_sample():
    b = Book()
    b.add("extra", 10, 0.1, opened="2026-11-12", closed="2026-11-13")
    assert b.verdict()["extras"]["n"] == 1


def test_pending_shows_progress_but_is_not_a_result():
    b = Book().many("extra", 19, 300, 6.0).many("taken", 60, -100, -2.0)
    v = b.verdict()
    assert v["verdict"] == "pending"
    assert v["remaining"] == 1
    assert v["extras"]["n"] == 19 and v["extras"]["avg_alpha_pct"] == 6.0
    assert "taken_lead_pp" not in v


def test_it_lands_on_the_20th_extra_close():
    b = Book().many("taken", 20, 50, 1.0).many("extra", 20, 50, 1.0)
    assert b.verdict()["verdict"] != "pending"


def test_closes_after_the_20th_extra_never_count():
    """Anti-renegotiation: later winners must not rescue the extras, and
    later losers must not sink the taken side."""
    b = Book().many("taken", 20, 100, 3.0).many("extra", 20, -100, -3.0)
    landed = b.verdict()
    b.many("extra", 10, 5000, 40.0).many("taken", 10, -5000, -40.0)
    v = b.verdict()
    assert v["verdict"] == landed["verdict"] == "extras_lag"
    assert v["extras"] == landed["extras"] and v["taken"] == landed["taken"]
    assert v["excluded"]["after_landing"] == 20


def test_taken_rows_before_the_landing_row_all_count():
    b = Book().many("extra", 19, 50, 1.0).many("taken", 35, 50, 1.0)
    b.add("extra", 50, 1.0)
    b.many("taken", 5, 50, 1.0)
    v = b.verdict()
    assert v["taken"]["n"] == 35 and v["extras"]["n"] == 20
    assert v["excluded"]["after_landing"] == 5


# --- the excluded classes -----------------------------------------------------

def test_unfilled_runaways_cannot_enter_either_group():
    """The DELL/CBRL lesson: two +15% unfilled rows made 'extras' look good."""
    b = Book().many("taken", 20, 20, 0.5).many("extra", 20, -100, -1.0)
    plain = b.verdict()
    b2 = Book()
    b2.many("unfilled", 5, 750, 15.0).many("taken_later", 3, 450, 9.5)
    b2.many("taken", 20, 20, 0.5).many("extra", 20, -100, -1.0)
    v = b2.verdict()
    assert v["verdict"] == plain["verdict"] == "no_case"
    assert v["extras"]["avg_alpha_pct"] == -1.0
    assert v["taken"]["avg_alpha_pct"] == 0.5
    assert v["excluded"]["unfilled"]["n"] == 5
    assert v["excluded"]["unfilled"]["total_pnl"] == 3750
    assert v["excluded"]["taken_later"]["n"] == 3


def test_unclassified_rows_are_listed_as_a_defect_and_never_counted():
    b = Book().many("unclassified", 25, 900, 18.0)
    v = b.verdict()
    assert v["verdict"] == "pending" and v["extras"]["n"] == 0
    assert v["excluded"]["unclassified"]["n"] == 25
    assert "triage" in v["defect"]


def test_no_defect_key_on_a_clean_sample():
    assert "defect" not in Book().many("extra", 3, 10, 0.1).verdict()


# --- the verdict --------------------------------------------------------------

def test_broad_extras_that_match_taken_earn_a_discussion():
    b = Book().many("taken", 30, 40, 0.8).many("extra", 20, 60, 1.2)
    v = b.verdict()
    assert v["verdict"] == "wider_book_discussion"
    assert v["taken_lead_pp"] == -0.4


def test_extras_equal_to_taken_is_enough():
    b = Book().many("taken", 20, 50, 1.0).many("extra", 20, 50, 1.0)
    assert b.verdict()["verdict"] == "wider_book_discussion"


def test_extras_just_below_taken_is_no_case():
    b = Book().many("taken", 20, 50, 1.0).many("extra", 20, 50, 0.9)
    v = b.verdict()
    assert v["verdict"] == "no_case"
    assert any("below taken" in r for r in v["reasons"])


def test_extras_whose_dollars_rest_on_one_trade_is_no_case():
    # alpha is fine with or without the best trade; only the dollar leg of
    # the fragility check can catch this one.
    b = Book().many("taken", 20, 10, 0.5).many("extra", 19, -20, 1.0)
    b.add("extra", 2000, 1.0, sym="MOON")
    v = b.verdict()
    assert v["extras"]["total_pnl"] > 0
    assert v["extras_avg_alpha_pct_ex_best"] == 1.0
    assert v["dropped_best"]["extras_pnl"] == "MOON"
    assert v["extras_total_pnl_ex_best"] == -380
    assert v["verdict"] == "no_case"
    assert v["reasons"] == ["extras total P&L without MOON -380 <= 0"]


def test_extras_whose_alpha_rests_on_one_trade_is_no_case():
    # dollars are fine with or without the best trade; only the alpha leg
    # can catch this one (and ex-best the extras still sit above taken).
    b = Book().many("taken", 20, -10, -0.5).many("extra", 19, 50, -0.3)
    b.add("extra", 50, 30.0, sym="SPIKE")
    v = b.verdict()
    assert v["extras"]["avg_alpha_pct"] > 0
    assert v["extras_total_pnl_ex_best"] > 0
    assert v["dropped_best"]["extras_alpha"] == "SPIKE"
    assert v["verdict"] == "no_case"
    assert v["reasons"] == ["extras avg alpha without SPIKE -0.3% <= 0"]


def test_extras_that_only_match_taken_thanks_to_one_trade_is_no_case():
    # ex-best the extras stay profitable and alpha-positive, but fall
    # below taken: the third leg of the fragility check on its own.
    b = Book().many("taken", 20, 50, 1.0).many("extra", 19, 40, 0.9)
    b.add("extra", 400, 8.0, sym="LIFT")
    v = b.verdict()
    assert v["extras"]["avg_alpha_pct"] >= v["taken"]["avg_alpha_pct"]
    assert v["extras_avg_alpha_pct_ex_best"] == 0.9
    assert v["verdict"] == "no_case"


def test_alpha_positive_extras_that_lost_dollars_are_no_case():
    b = Book().many("taken", 20, -200, -1.0).many("extra", 20, -50, 0.5)
    v = b.verdict()
    assert v["verdict"] == "no_case"
    assert v["reasons"] == ["extras total P&L -1000 <= 0"]


def test_profitable_extras_with_negative_alpha_are_no_case():
    b = Book().many("taken", 20, 30, -1.0).many("extra", 20, 50, -0.5)
    v = b.verdict()
    assert v["verdict"] == "no_case"
    assert v["reasons"] == ["extras avg alpha -0.5% <= 0"]


def test_taken_ahead_by_the_margin_is_extras_lag():
    b = Book().many("taken", 20, 50, 1.0).many("extra", 20, -50, -1.0)
    v = b.verdict()
    assert v["verdict"] == "extras_lag"
    assert v["taken_lead_pp"] == 2.0


def test_taken_ahead_by_less_than_the_margin_is_no_case():
    b = Book().many("taken", 20, 50, 0.9).many("extra", 20, -50, -1.0)
    v = b.verdict()
    assert v["taken_lead_pp"] == 1.9
    assert v["verdict"] == "no_case"


def test_a_taken_lead_resting_on_one_trade_is_no_case():
    b = Book()
    b.add("taken", 3000, 60.0, sym="ONE")
    b.many("taken", 19, -50, -1.0).many("extra", 20, -50, -1.0)
    v = b.verdict()
    assert v["taken_lead_pp"] >= 2.0
    assert v["taken_lead_pp_ex_taken_best"] == 0.0
    assert v["verdict"] == "no_case"
    assert any("rests on ONE" in r for r in v["reasons"])


def test_too_few_taken_closes_is_undeterminable_not_a_win_for_extras():
    b = Book().many("taken", 19, -50, -1.0).many("extra", 20, 100, 2.0)
    v = b.verdict()
    assert v["verdict"] == "undeterminable"
    assert "19 taken" in v["reasons"][0]


def test_missing_alpha_is_never_read_as_passing():
    b = Book().many("taken", 20, 50, 1.0)
    b.many("extra", 15, 100, 2.0).many("extra", 5, 100, None)
    assert b.verdict()["verdict"] == "undeterminable"
    # exactly four fifths covered is enough to judge
    b = Book().many("taken", 20, 50, 1.0)
    b.many("extra", 16, 100, 2.0).many("extra", 4, 100, None)
    v = b.verdict()
    assert v["verdict"] == "wider_book_discussion"
    assert v["extras"]["alpha_n"] == 16


def test_group_stats_report_return_on_the_fixed_notional():
    b = Book()
    b.add("extra", 250, 4.0)     # 50 sh x $100 -> +5.0%
    b.add("extra", -100, -3.0)   # -2.0%
    g = b.verdict()["extras"]
    assert g["avg_return_pct"] == 1.5
    assert g["wins"] == 1 and g["total_pnl"] == 150
    assert g["symbols"] == [r["symbol"] for r in b.rows]


# --- independence and wiring --------------------------------------------------

def test_the_first_wide_verdict_is_untouched():
    """Two registrations, two artifacts: nothing in the new module is
    imported by, or imports from, the first verdict's code."""
    import inspect

    import autoswing.shadow as shadow

    assert "wide_extras" not in inspect.getsource(shadow)
    assert "from .shadow" not in inspect.getsource(wx)
    assert "import shadow" not in inspect.getsource(wx)
    assert shadow.WIDE_VERDICT_N == 40
    assert shadow.WIDE_VERDICT_REGISTERED == "2026-10-14"


def test_safe_wrapper_reports_an_error_state_not_a_quiet_pending(tmp_path):
    (tmp_path / "2026-11-16.jsonl").write_text('{"event": "gate.decision", "ts\n')
    v = wide_extras_verdict_safe([], tmp_path)
    assert v["verdict"] == "error"
    assert v["status"].startswith("ERROR")
    assert "JSONDecodeError" in v["error"]
    assert v["registered"] == "2026-11-12"


def test_shadow_status_carries_both_wide_blocks(tmp_path, monkeypatch):
    import autoswing.config as cfg
    from autoswing.commands import shadow as shadow_cmd

    (tmp_path / "journal").mkdir()
    t = "2026-11-16T15:09:00+00:00"
    (tmp_path / "journal" / "2026-11-16.jsonl").write_text("\n".join([
        gate(t, "TKN"),
        jl(t, "broker.place_bracket_order", result={
            "symbol": "TKN", "orders": [{"role": "entry", "status": "Filled"}]}),
        jl(t, "shadow.proposal", proposal={"symbol": "TKN"},
           result={"wide": True, "opened_virtual": True}),
        jl(t, "shadow.proposal", proposal={"symbol": "XTR"},
           result={"wide": True, "opened_virtual": True}),
    ]) + "\n")
    wide_ledger = tmp_path / "ledger-True.jsonl"
    wide_ledger.write_text("\n".join(json.dumps(r) for r in [
        row("TKN", 100, 1.0), row("XTR", -50, -1.0),
        row("OLD", 900, 9.0, opened="2026-11-03", closed="2026-11-13"),
    ]) + "\n")
    monkeypatch.setattr(cfg, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        shadow_cmd, "_shadow_paths",
        lambda wide: (tmp_path / f"pos-{wide}.json",
                      tmp_path / f"ledger-{wide}.jsonl"))
    wide = shadow_cmd._shadow_status()["wide"]
    assert wide["preregistered_verdict"]["verdict"] == "pending"
    v = wide["extras_preregistered_verdict"]
    assert v["verdict"] == "pending" and v["remaining"] == 19
    assert v["taken"]["n"] == 1 and v["extras"]["symbols"] == ["XTR"]
    assert v["excluded"]["opened_before_registration"] == 1
