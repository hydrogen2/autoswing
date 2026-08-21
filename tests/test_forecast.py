"""Forecast ledger tests: validation, immutability semantics, conservative
scoring, calibration stats."""

from autoswing.forecast import (
    awaiting_actuals,
    classify_eps,
    classify_reaction,
    compute_stats,
    post_hoc_reason,
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

    def test_unknown_leg_excluded_from_hit_rate_not_counted_wrong(self):
        # Regression (VST/TTWO 2026-08-07): scored before the calendar
        # published actuals — eps_actual "unknown", eps_correct False — and
        # the forced miss was pooled into the EPS hit rate. An unmeasured
        # leg must drop out of that leg's denominator entirely.
        scores = [
            {"forecast_id": "VST-d", "tier": "quick", "eps_actual": "unknown",
             "eps_correct": False, "reaction_actual": "flat",
             "reaction_correct": False, "confidence": 0.55, "scorable": True},
            {"forecast_id": "OK-d", "tier": "quick", "eps_actual": "beat",
             "eps_correct": True, "reaction_actual": "up",
             "reaction_correct": True, "confidence": 0.6, "scorable": True},
        ]
        st = compute_stats([], scores)
        q = st["tiers"]["quick"]
        assert q["n_scored"] == 2
        assert q["eps_n"] == 1 and q["eps_hit_rate"] == 1.0
        assert q["reaction_n"] == 2 and q["reaction_hit_rate"] == 0.5
        # calibration only over reaction-known rows (both are: flat is known)
        assert q["calibration"]["50-60"]["n"] == 1
        assert q["calibration"]["60-70"]["n"] == 1


class TestAwaitingActuals:
    def test_partial_actuals_defer_within_grace(self):
        # Reaction known but EPS unpublished: scoring now burns the EPS leg
        # forever (one score event per forecast id). Wait out the grace.
        assert awaiting_actuals(None, 4.9, grace_expired=False)
        assert awaiting_actuals(3.0, None, grace_expired=False)
        assert awaiting_actuals(None, None, grace_expired=False)

    def test_grace_expiry_scores_what_is_known(self):
        assert not awaiting_actuals(None, 4.9, grace_expired=True)
        assert not awaiting_actuals(3.0, 4.9, grace_expired=False)


class TestRetiredQuickReactionLeg:
    """Quick tier stopped forecasting reactions 2026-08-20 (n=46, 41.3%,
    inverted calibration). The leg must be optional there, still required
    for deep, and a missing leg must be EXCLUDED from the denominator —
    not scored wrong."""

    def payload(self, **kw):
        base = dict(symbol="X", report_date="2026-08-21", timing="bmo",
                    tier="quick", eps_call="beat", confidence=0.6,
                    reasoning="peer read-through")
        base.update(kw)
        return base

    def test_quick_may_omit_reaction(self):
        assert validate_forecast(self.payload()) == []

    def test_quick_rejects_a_bad_reaction_value(self):
        assert validate_forecast(self.payload(reaction_call="sideways"))

    def test_quick_still_accepts_an_explicit_reaction(self):
        assert validate_forecast(self.payload(reaction_call="up")) == []

    def test_deep_still_requires_reaction(self):
        assert validate_forecast(self.payload(tier="deep"))
        assert validate_forecast(self.payload(tier="deep", reaction_call="up")) == []

    def test_missing_leg_scores_as_not_forecast(self):
        s = score_forecast({"id": "X-1", "tier": "quick", "eps_call": "beat",
                            "confidence": 0.6}, 8.0, 3.0, "now")
        assert s["reaction_actual"] == "not_forecast"
        assert s["reaction_correct"] is False
        assert s["eps_correct"] is True
        assert s["scorable"] is True

    def test_not_forecast_excluded_from_denominator(self):
        scored = [
            {"forecast_id": "A", "tier": "quick", "scorable": True,
             "eps_actual": "beat", "eps_correct": True,
             "reaction_actual": "not_forecast", "reaction_correct": False,
             "confidence": 0.6},
            {"forecast_id": "B", "tier": "quick", "scorable": True,
             "eps_actual": "beat", "eps_correct": True,
             "reaction_actual": "up", "reaction_correct": True,
             "confidence": 0.6},
        ]
        q = compute_stats([], scored)["tiers"]["quick"]
        assert q["eps_n"] == 2 and q["eps_hit_rate"] == 1.0
        # only the forecast that HAD a leg counts toward the reaction rate
        assert q["reaction_n"] == 1 and q["reaction_hit_rate"] == 1.0


class TestPostHocGuard:
    """Look-ahead guard: a "forecast" logged after the print is transcription.

    Regression for the 2026-08-21 finding — PFGC/TRMB/GLBE/AMCR were all
    logged 2026-08-12 at 08:04 ET for same-day BMO reports, i.e. possibly
    after the release. They scored badly so no hit rate was inflated, but
    nothing in the code prevented it.
    """

    def at(self, s):
        from datetime import datetime
        from autoswing.risk_gate import ET
        return datetime.fromisoformat(s).replace(tzinfo=ET)

    def test_future_report_is_fine(self):
        assert post_hoc_reason("2026-08-12", "bmo", self.at("2026-08-11T08:04")) is None
        assert post_hoc_reason("2026-08-12", "amc", self.at("2026-08-11T08:04")) is None

    def test_same_day_bmo_refused(self):
        # the historical PFGC/TRMB/GLBE/AMCR shape
        r = post_hoc_reason("2026-08-12", "bmo", self.at("2026-08-12T08:04"))
        assert r and "BMO" in r

    def test_same_day_bmo_refused_even_before_dawn(self):
        # releases land from 06:00 ET; "early enough" is not knowable
        assert post_hoc_reason("2026-08-12", "bmo", self.at("2026-08-12T05:00"))

    def test_same_day_amc_allowed_during_session(self):
        # CBRS 2026-08-12: same-day AMC logged premarket is a real prediction
        assert post_hoc_reason("2026-08-12", "amc", self.at("2026-08-12T08:04")) is None
        assert post_hoc_reason("2026-08-12", "amc", self.at("2026-08-12T15:59")) is None

    def test_same_day_amc_refused_from_the_close(self):
        assert post_hoc_reason("2026-08-12", "amc", self.at("2026-08-12T16:00"))
        assert post_hoc_reason("2026-08-12", "amc", self.at("2026-08-12T18:30"))

    def test_same_day_unknown_timing_refused(self):
        r = post_hoc_reason("2026-08-12", "unknown", self.at("2026-08-12T08:04"))
        assert r and "unknown timing" in r

    def test_past_report_refused_for_every_timing(self):
        for timing in ("bmo", "amc", "unknown"):
            assert post_hoc_reason("2026-08-11", timing, self.at("2026-08-12T08:04"))
