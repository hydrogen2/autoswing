"""Pre-registered post-#25 out-of-sample v2 verdict (registered 2026-11-06).

Same anti-renegotiation intent as the first-25 tests: pin the sample
boundary on BOTH sides (the first 25 never count; closes after the
period's 25th never count), and pin that the two registrations are
independent — neither verdict can move the other.
"""

from autoswing.shadow import (
    V2_OOS_VERDICT_N, V2_VERDICT_N, v2_oos_verdict, v2_verdict,
)


def row(pnl, alpha, closed="2026-11-10"):
    return {"pnl": pnl, "alpha_pct": alpha, "closed": closed}


FIRST25 = [row(-40, -1.0, closed="2026-10-01")] * V2_VERDICT_N


def test_period_opens_empty_after_the_first_25():
    v = v2_oos_verdict(FIRST25)
    assert v["verdict"] == "pending"
    assert v["sample_n"] == 0
    assert v["period_closed"] == 0
    assert v["remaining"] == V2_OOS_VERDICT_N


def test_the_first_25_never_count_toward_the_oos_sample():
    # A catastrophic first 25 with a clean positive OOS period: the OOS
    # verdict must see only the period's rows.
    oos = [row(40, 0.8)] * V2_OOS_VERDICT_N
    v = v2_oos_verdict(FIRST25 + oos)
    assert v["sample_n"] == V2_OOS_VERDICT_N
    assert v["total_pnl"] == 40 * V2_OOS_VERDICT_N
    assert v["verdict"] == "promotion_discussion"


def test_closes_after_the_periods_25th_never_count():
    """The anti-renegotiation test, mirrored from the first sample: winners
    arriving after the OOS period's 25th close must not rescue a bin."""
    oos = [row(-40, -1.0)] * V2_OOS_VERDICT_N
    later = [row(5000, 40.0)] * 10
    v = v2_oos_verdict(FIRST25 + oos + later)
    assert v["verdict"] == "bin"
    assert v["sample_n"] == V2_OOS_VERDICT_N
    assert v["period_closed"] == V2_OOS_VERDICT_N + 10


def test_registrations_are_independent_in_both_directions():
    """Neither verdict reads the other's rows: first-25 stays binned however
    the OOS period goes, and the OOS verdict is identical whatever the first
    25 looked like."""
    oos = [row(40, 0.8)] * V2_OOS_VERDICT_N
    assert v2_verdict(FIRST25 + oos)["verdict"] == "bin"
    great_first25 = [row(40, 0.8, closed="2026-10-01")] * V2_VERDICT_N
    assert v2_oos_verdict(FIRST25 + oos) == v2_oos_verdict(great_first25 + oos)


def test_pending_shows_progress_but_is_not_a_result():
    # The live state at registration: CRL and HHH, 2/25.
    oos = [row(-330.54, -3.45, closed="2026-11-05"),
           row(219.09, 6.45, closed="2026-11-03")]
    v = v2_oos_verdict(FIRST25 + oos)
    assert v["verdict"] == "pending"
    assert v["sample_n"] == 2
    assert v["remaining"] == V2_OOS_VERDICT_N - 2
    assert v["total_pnl"] == -111.45
    assert "not a result" in v["note"]


def test_same_criteria_negative_alpha_bins_despite_positive_pnl():
    oos = [row(100, -0.5)] * V2_OOS_VERDICT_N
    v = v2_oos_verdict(FIRST25 + oos)
    assert v["verdict"] == "bin"
    assert any("alpha" in r for r in v["reasons"])


def test_same_criteria_one_outlier_cannot_carry_the_period():
    oos = [row(-20, -0.3)] * (V2_OOS_VERDICT_N - 1) + [row(2000, 30.0)]
    v = v2_oos_verdict(FIRST25 + oos)
    assert v["total_pnl"] > 0 and v["avg_alpha_pct"] > 0
    assert v["verdict"] == "no_promotion_rests_on_one_trade"


def test_missing_alpha_is_never_read_as_passing():
    oos = [{"pnl": 50, "closed": "2026-11-10"}] * V2_OOS_VERDICT_N
    assert v2_oos_verdict(FIRST25 + oos)["verdict"] == "undeterminable"


# --- owner alert for the OOS verdict (ratified 2026-11-10) -------------------
# "Alert me before dropping it" covers every verdict on the book, not just
# the first. The first-25 alert was sent 2026-11-02; these pin that its
# marker can never silence the out-of-sample one.

import importlib.util
import json
from pathlib import Path

from autoswing.shadow import oos_verdict_alert

OOS_BIN = FIRST25 + [row(-40, -1.0)] * V2_OOS_VERDICT_N


def test_oos_alert_silent_while_pending():
    assert oos_verdict_alert(v2_oos_verdict(FIRST25 + [row(-40, -1.0)] * 2),
                             None) is None


def test_oos_alert_fires_once_and_says_nothing_was_stopped():
    v = v2_oos_verdict(OOS_BIN)
    body = oos_verdict_alert(v, None)
    assert "OUT-OF-SAMPLE verdict = BIN" in body
    assert "NOTHING HAS BEEN STOPPED" in body
    assert "Closes #26-#50" in body
    assert oos_verdict_alert(v, {"verdict": "bin"}) is None


def test_oos_alert_fires_again_if_the_verdict_changes():
    v = v2_oos_verdict(FIRST25 + [row(40, 0.8)] * V2_OOS_VERDICT_N)
    assert v["verdict"] == "promotion_discussion"
    body = oos_verdict_alert(v, {"verdict": "bin"})
    assert body is not None and "Nothing is" in body


def _alert_script(tmp_path, monkeypatch, rows, send_rc=0):
    path = Path(__file__).resolve().parents[1] / "scripts" / "v2_verdict_alert.py"
    spec = importlib.util.spec_from_file_location("v2_verdict_alert_t", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    shadow = tmp_path / "state" / "shadow"
    shadow.mkdir(parents=True)
    (tmp_path / "state" / "reports").mkdir()
    (shadow / "ledger.jsonl").write_text(
        "".join(json.dumps({"event": "shadow.close", **r}) + "\n" for r in rows))
    sent = []
    monkeypatch.setattr(mod, "REPO", tmp_path)
    monkeypatch.setattr(mod, "SHADOW", shadow)
    monkeypatch.setattr(mod, "CHECKS", tuple(
        (label, vf, af, shadow / marker.name, suffix, tag)
        for label, vf, af, marker, suffix, tag in mod.CHECKS))
    monkeypatch.setattr(mod, "_send",
                        lambda subject, p: sent.append(subject) or send_rc)
    return mod, shadow, sent


def test_first_25_marker_does_not_silence_the_oos_alert(tmp_path, monkeypatch):
    mod, shadow, sent = _alert_script(tmp_path, monkeypatch, OOS_BIN)
    # The live state since 2026-11-02: first-25 already alerted as "bin".
    (shadow / "v2_verdict_alerted.json").write_text('{"verdict": "bin"}')
    assert mod.main() == 0
    assert len(sent) == 1 and "OUT-OF-SAMPLE" in sent[0]
    marker = json.loads((shadow / "v2_oos_verdict_alerted.json").read_text())
    assert marker["verdict"] == "bin" and marker["sample_n"] == V2_OOS_VERDICT_N
    # Second night: nothing new, nothing sent.
    assert mod.main() == 0 and len(sent) == 1


def test_failed_send_leaves_no_marker_so_it_retries(tmp_path, monkeypatch):
    mod, shadow, sent = _alert_script(tmp_path, monkeypatch, OOS_BIN, send_rc=1)
    (shadow / "v2_verdict_alerted.json").write_text('{"verdict": "bin"}')
    assert mod.main() != 0
    assert not (shadow / "v2_oos_verdict_alerted.json").exists()
    # Body is on disk for the manager's report even though the email failed.
    assert list((tmp_path / "state" / "reports").glob("*-V2-OOS-VERDICT.md"))


def test_pending_period_sends_nothing(tmp_path, monkeypatch):
    mod, shadow, sent = _alert_script(
        tmp_path, monkeypatch, FIRST25 + [row(-40, -1.0)] * 2)
    (shadow / "v2_verdict_alerted.json").write_text('{"verdict": "bin"}')
    assert mod.main() == 0 and sent == []
