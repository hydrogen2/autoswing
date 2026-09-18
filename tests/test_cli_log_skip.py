"""Regression: log-skip must persist validated entry/stop geometry.

Found 2026-09-18: validate_skip accepts an optional entry/stop pair so the
stop_geometry counterfactual can replay the brain's REAL declined geometry
("logged" basis), but _log_skip rebuilt the row from only
symbol/date/category/reason — silently dropping the pair after validating
it. Four weeks of stop_geometry skips (FPS 09-18: 38.40/32.90 passed, then
replayed as a 39.44/36.31 reconstruction) accrued zero rows toward the
n>=20 logged-geometry verdict.
"""

import json
from types import SimpleNamespace

import autoswing.config as config
from autoswing.commands import research
from autoswing.journal import Journal


def _log_skip(tmp_path, monkeypatch, payload):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    (tmp_path / "state" / "research").mkdir(parents=True)
    src = tmp_path / "in.json"
    src.write_text(json.dumps(payload))
    args = SimpleNamespace(skip=str(src))
    out = research._log_skip(args, Journal(tmp_path / "journal"))
    rows = [json.loads(l) for l in
            (tmp_path / "state" / "research" / "skips.jsonl")
            .read_text().splitlines()]
    return out, rows


BASE = {
    "symbol": "fps",
    "date": "2026-09-18",
    "category": "stop_geometry",
    "reason": "chart stop ~14% below entry, past the 12% ceiling",
}


class TestLogSkipGeometry:
    def test_entry_and_stop_persisted(self, tmp_path, monkeypatch):
        out, rows = _log_skip(
            tmp_path, monkeypatch, {**BASE, "entry": 38.40, "stop": 32.90})
        assert out["logged"] == "FPS-2026-09-18"
        assert rows[0]["entry"] == 38.40
        assert rows[0]["stop"] == 32.90

    def test_geometry_omitted_stays_omitted(self, tmp_path, monkeypatch):
        _, rows = _log_skip(tmp_path, monkeypatch, dict(BASE))
        assert "entry" not in rows[0] and "stop" not in rows[0]

    def test_persisted_geometry_replays_as_logged_basis(
            self, tmp_path, monkeypatch):
        import pandas as pd
        from autoswing.research import replay_skip

        _, rows = _log_skip(
            tmp_path, monkeypatch, {**BASE, "entry": 38.40, "stop": 32.90})
        idx = pd.bdate_range("2026-09-18", periods=5)
        df = pd.DataFrame(
            {"Open": 38.0, "High": 39.0, "Low": 37.5, "Close": 38.5},
            index=idx)
        r = replay_skip(rows[0], df, 15, idx[-1].date())
        assert r["basis"] == "logged"
        assert r["entry"] == 38.40 and r["stop"] == 32.90
