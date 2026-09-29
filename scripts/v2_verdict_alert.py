#!/usr/bin/env python3
"""Email the owner once when the pre-registered news-v2 verdict lands.

Owner ruling 2026-09-29: "alert me before dropping it, I may want to keep it
longer". Run nightly from manager_run.sh AFTER the review, independently of
the manager model: an alert that depends on an agent remembering to raise it
is the unkeepable-promise pattern. Never stops or alters the book.
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from autoswing.shadow import load_ledger, v2_verdict, verdict_alert  # noqa: E402

MARKER = REPO / "state" / "shadow" / "v2_verdict_alerted.json"


def main() -> int:
    verdict = v2_verdict(load_ledger(REPO / "state" / "shadow" / "ledger.jsonl"))
    prior = json.loads(MARKER.read_text()) if MARKER.exists() else None
    body = verdict_alert(verdict, prior)
    if body is None:
        print(f"v2 verdict: {verdict['verdict']} ({verdict['sample_n']}/"
              f"{verdict['required_n']}) — no alert")
        return 0

    today = datetime.now(timezone.utc).date().isoformat()
    path = REPO / "state" / "reports" / f"{today}-V2-VERDICT.md"
    path.write_text(body)
    rc = subprocess.call([sys.executable, str(REPO / "scripts" / "send_report.py"),
                          "--subject",
                          f"autoswing DECISION: news-v2 verdict = "
                          f"{verdict['verdict'].upper()} — book still running, "
                          "your call",
                          "--body-file", str(path)])
    if rc != 0:
        # Marker NOT written, so tomorrow's run retries the email; the body
        # is on disk for the manager's report either way.
        print(f"alert email failed (rc={rc}); saved {path}; will retry")
        return rc
    MARKER.write_text(json.dumps({"verdict": verdict["verdict"],
                                  "alerted": today,
                                  "sample_n": verdict["sample_n"]}) + "\n")
    print(f"alerted owner: {verdict['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
