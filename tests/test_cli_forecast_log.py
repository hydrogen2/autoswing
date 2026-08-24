"""Regression: forecast-log must accept a quick-tier forecast omitting
reaction_call.

On 2026-08-24 premarket the brain followed the playbook (quick tier logs
EPS-only since the reaction leg was retired 2026-08-20) and forecast-log
crashed with KeyError: 'reaction_call'. validate_forecast explicitly
allows the omission; the Forecast constructor call used direct key access.
The workaround was passing an explicit null — the omission form must work.
"""

import json
from types import SimpleNamespace

from autoswing.commands import research
from autoswing.journal import Journal


def _log(tmp_path, monkeypatch, payload):
    monkeypatch.setattr(
        research, "_forecast_paths",
        lambda: (tmp_path / "forecasts.jsonl", tmp_path / "scores.jsonl"),
    )
    src = tmp_path / "in.json"
    src.write_text(json.dumps(payload))
    args = SimpleNamespace(forecast=str(src))
    return research._forecast_log(args, Journal(tmp_path / "journal"))


QUICK = {
    "symbol": "smtc",
    "report_date": "2099-01-15",   # far future: never post-hoc
    "timing": "amc",
    "tier": "quick",
    "eps_call": "beat",
    "confidence": 0.6,
    "reasoning": "quick pass: consensus momentum",
}


class TestQuickTierOmitsReactionCall:
    def test_omitted_reaction_call_logs_as_none(self, tmp_path, monkeypatch):
        out = _log(tmp_path, monkeypatch, dict(QUICK))
        assert out == {"logged": "SMTC-2099-01-15", "tier": "quick"}
        rows = [json.loads(l) for l in
                (tmp_path / "forecasts.jsonl").read_text().splitlines()]
        assert rows[0]["reaction_call"] is None

    def test_explicit_null_still_works(self, tmp_path, monkeypatch):
        out = _log(tmp_path, monkeypatch,
                   {**QUICK, "symbol": "box", "reaction_call": None})
        assert out["logged"] == "BOX-2099-01-15"

    def test_deep_tier_reaction_call_preserved(self, tmp_path, monkeypatch):
        _log(tmp_path, monkeypatch,
             {**QUICK, "symbol": "dks", "tier": "deep", "reaction_call": "up"})
        rows = [json.loads(l) for l in
                (tmp_path / "forecasts.jsonl").read_text().splitlines()]
        assert rows[0]["reaction_call"] == "up"
