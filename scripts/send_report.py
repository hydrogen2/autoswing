#!/usr/bin/env python3
"""Send a report email via SMTP. Credentials from .secrets.env (gitignored).

Usage: send_report.py --subject "..." --body-file path.md
Falls back with a clear nonzero exit if credentials are missing so callers
can save the report to disk instead.
"""

import argparse
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path

SECRETS = Path(__file__).resolve().parents[1] / ".secrets.env"


def load_secrets() -> dict:
    if not SECRETS.exists():
        sys.exit(f"no {SECRETS} — email not configured")
    out = {}
    for line in SECRETS.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", required=True)
    ap.add_argument("--body-file", required=True)
    args = ap.parse_args()

    s = load_secrets()
    for key in ("SMTP_USER", "SMTP_PASS", "REPORT_TO"):
        if key not in s:
            sys.exit(f"missing {key} in {SECRETS}")

    msg = MIMEText(Path(args.body_file).read_text(), "plain", "utf-8")
    msg["Subject"] = args.subject
    msg["From"] = s["SMTP_USER"]
    msg["To"] = s["REPORT_TO"]

    with smtplib.SMTP(s.get("SMTP_HOST", "smtp.gmail.com"),
                      int(s.get("SMTP_PORT", "587")), timeout=30) as smtp:
        smtp.starttls()
        smtp.login(s["SMTP_USER"], s["SMTP_PASS"])
        smtp.send_message(msg)
    print("sent")


if __name__ == "__main__":
    main()
