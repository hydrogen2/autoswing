"""Forecast ledger tests: validation, immutability semantics, conservative
scoring, calibration stats."""

from autoswing.forecast import (
    classify_eps,
    classify_reaction,
    compute_stats,
    score_forecast,
    validate_forecast,
)


def payload(**overrides):
    base = dict(
        symbol="XOM", report_date="2026-08-10", timing="amc", tier="deep",
        eps_call="beat", reaction_call="up", confidence=0.7,
        reasoning="peers CVX and SHEL both beat on refining margins",
    )
    base.update(overrides)
    return base


class TestValidation:
    def test_valid_passes(self):
        assert validate_forecast(payload()) == []

    def test_bad_call_rejected(self):
        assert any("eps_call" in e for e in validate_forecast(payload(eps_call="moon")))

    def test_overconfident_rejected(self):
        assert any("confidence" in e for e in validate_forecast(payload(confidence=1.5)))

    def test_underconfident_rejected(self):
        # Below 0.5 means you believe the opposite call — log that instead.
        assert any("confidence" in e for e in validate_forecast(payload(confidence=0.3)))

    def test_no_reasoning_rejected(self):
        assert any("reasoning" in e for e in validate_forecast(payload(reasoning="")))


class TestClassification:
    def test_eps_bands(self):
        assert classify_eps(8.3) == "beat"
        assert classify_eps(-4.0) == "miss"
        assert classify_eps(1.5) == "inline"
        assert classify_eps(-2.0) == "inline"
        assert classify_eps(None) == "unknown"

    def test_reaction_bands(self):
        assert classify_reaction(4.2) == "up"
        assert classify_reaction(-1.0) == "down"
        assert classify_reaction(0.4) == "flat"


class TestScoring:
    def fc(self, **kw):
        base = dict(id="XOM-2026-08-10", tier="deep", eps_call="beat",
                    reaction_call="up", confidence=0.7)
        base.update(kw)
        return base

    def test_both_correct(self):
        s = score_forecast(self.fc(), surprise_pct=9.0, move_pct=3.2,
                           scored_at="t")
        assert s["eps_correct"] and s["reaction_correct"] and s["scorable"]

    def test_flat_reaction_scores_updown_call_wrong(self):
        s = score_forecast(self.fc(), surprise_pct=9.0, move_pct=0.3,
                           scored_at="t")
        assert s["eps_correct"] and not s["reaction_correct"]
        assert s["reaction_actual"] == "flat"

    def test_missing_actuals_unscorable(self):
        s = score_forecast(self.fc(), surprise_pct=None, move_pct=None,
                           scored_at="t")
        assert not s["scorable"]

    def test_beat_call_on_miss_wrong(self):
        s = score_forecast(self.fc(), surprise_pct=-10.0, move_pct=-5.0,
                           scored_at="t")
        assert not s["eps_correct"] and not s["reaction_correct"]


class TestStats:
    def test_tiers_separated_and_calibrated(self):
        forecasts = [
            {"id": f"A{i}-d", "tier": "deep"} for i in range(4)
        ] + [{"id": "B-q", "tier": "quick"}]
        scores = [
            {"forecast_id": "A0-d", "tier": "deep", "eps_correct": True,
             "reaction_correct": True, "confidence": 0.75, "scorable": True},
            {"forecast_id": "A1-d", "tier": "deep", "eps_correct": False,
             "reaction_correct": False, "confidence": 0.75, "scorable": True},
            {"forecast_id": "A2-d", "tier": "deep", "eps_correct": True,
             "reaction_correct": True, "confidence": 0.55, "scorable": True},
            {"forecast_id": "B-q", "tier": "quick", "eps_correct": True,
             "reaction_correct": False, "confidence": 0.6, "scorable": True},
        ]
        st = compute_stats(forecasts, scores)
        assert st["pending"] == 1  # A3-d never scored
        assert st["tiers"]["deep"]["n_scored"] == 3
        assert st["tiers"]["deep"]["reaction_hit_rate"] == round(2 / 3, 3)
        assert st["tiers"]["quick"]["reaction_hit_rate"] == 0.0
        assert st["tiers"]["deep"]["calibration"]["70-80"]["n"] == 2

    def test_empty(self):
        st = compute_stats([], [])
        assert st["tiers"]["deep"]["n_scored"] == 0
