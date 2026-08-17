"""gate-status must state the live dollar caps the gate enforces — the
brain oversized ENS (08-14) and HTHT (08-17) working from a remembered
percentage instead of the configured one."""

import pytest

from autoswing.commands.trading import _sizing_caps

from test_risk_gate import RTH, account, failed_rules, make_gate, proposal


class TestSizingCapsTelemetry:
    def test_caps_match_what_evaluate_enforces(self, tmp_path):
        gate = make_gate(tmp_path, {"max_position_pct": 10.0})
        status = gate.status(account(), now=RTH)
        caps = _sizing_caps(gate.cfg, status["virtual_equity"])

        assert caps["max_position_pct"] == 10.0
        assert caps["max_position_notional"] == pytest.approx(
            status["virtual_equity"] * 0.10, abs=0.01
        )
        # A proposal at the stated notional cap passes; one above fails.
        price = 100.0
        ok_qty = int(caps["max_position_notional"] // price)
        d = gate.evaluate(proposal(quantity=ok_qty), account(), now=RTH)
        assert "max_position_size" not in failed_rules(d)
        d = gate.evaluate(proposal(quantity=ok_qty + 5), account(), now=RTH)
        assert "max_position_size" in failed_rules(d)

    def test_risk_budget_and_exposure_fields(self, tmp_path):
        gate = make_gate(tmp_path)
        status = gate.status(account(), now=RTH)
        caps = _sizing_caps(gate.cfg, status["virtual_equity"])
        assert caps["risk_budget_dollars"] == pytest.approx(
            status["virtual_equity"] * 0.01, abs=0.01
        )
        assert caps["max_gross_exposure_dollars"] == pytest.approx(
            status["virtual_equity"] * 1.0, abs=0.01
        )
