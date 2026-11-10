#!/usr/bin/env python3
"""Email the owner once when a pre-registered news-v2 verdict lands.

Owner ruling 2026-09-29: "alert me before dropping it, I may want to keep it
longer". Run nightly from manager_run.sh AFTER the review, independently of
the manager model: an alert that depends on an agent remembering to raise it
is the unkeepable-promise pattern. Never stops or alters the book.

Two verdicts, checked independently (2026-11-10): the first-25 sample
(alerted 2026-11-02) and the post-#25 out-of-sample period the owner opened
by keeping the book. Each has its own marker, so the alert already sent for
the first can never silence the second, and a failed send for one does not
skip the other.
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from autoswing.shadow import (  # noqa: E402
    load_ledger, oos_verdict_alert, v2_oos_verdict, v2_verdict, verdict_alert,
)

SHADOW = REPO / "state" / "shadow"
MARKER = SHADOW / "v2_verdict_alerted.json"
OOS_MARKER = SHADOW / "v2_oos_verdict_alerted.json"

# (label, verdict fn, alert-body fn, marker, report suffix, subject tag)
CHECKS = (
    ("v2 verdict", v2_verdict, verdict_alert, MARKER,
     "V2-VERDICT", "news-v2 verdict"),
    ("v2 OOS verdict", v2_oos_verdict, oos_verdict_alert, OOS_MARKER,
     "V2-OOS-VERDICT", "news-v2 OUT-OF-SAMPLE verdict"),
)


def _send(subject: str, path: Path) -> int:
    return subprocess.call([sys.executable,
                            str(REPO / "scripts" / "send_report.py"),
                            "--subject", subject, "--body-file", str(path)])


def check(label, verdict_fn, alert_fn, marker, suffix, tag, rows) -> int:
    verdict = verdict_fn(rows)
    prior = json.loads(marker.read_text()) if marker.exists() else None
    body = alert_fn(verdict, prior)
    if body is None:
        print(f"{label}: {verdict['verdict']} ({verdict['sample_n']}/"
              f"{verdict['required_n']}) — no alert")
        return 0

    today = datetime.now(timezone.utc).date().isoformat()
    path = REPO / "state" / "reports" / f"{today}-{suffix}.md"
    path.write_text(body)
    rc = _send(f"autoswing DECISION: {tag} = {verdict['verdict'].upper()} — "
               "book still running, your call", path)
    if rc != 0:
        # Marker NOT written, so tomorrow's run retries the email; the body
        # is on disk for the manager's report either way.
        print(f"{label}: alert email failed (rc={rc}); saved {path}; will retry")
        return rc
    marker.write_text(json.dumps({"verdict": verdict["verdict"],
                                  "alerted": today,
                                  "sample_n": verdict["sample_n"]}) + "\n")
    print(f"{label}: alerted owner: {verdict['verdict']}")
    return 0


def main() -> int:
    rows = load_ledger(SHADOW / "ledger.jsonl")
    worst = 0
    for spec in CHECKS:
        worst = max(worst, abs(check(*spec, rows)))
    return worst


if __name__ == "__main__":
    sys.exit(main())
