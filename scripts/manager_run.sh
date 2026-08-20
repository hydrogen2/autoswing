#!/usr/bin/env bash
# Daily manager/dev review run. Invoked by cron after the close.
set -euo pipefail

REPO=/home/supper-user/autoswing
LOGDIR="$REPO/state/brain/logs"
LOCK=/tmp/autoswing-brain.lock
export PATH="$HOME/.local/bin:$PATH"

# Model is PINNED, not inherited. Two reasons: (1) an account-level default
# change must never silently alter trading behaviour mid-experiment — the
# lessons / wide-ledger / forecast series are only comparable within a model
# regime; (2) the owner's interactive /model choice does not reach cron.
# Changed Sonnet 5 -> Opus 5 on 2026-08-20 (see journal + docs/model-history).
MODEL="${AUTOSWING_MODEL:-claude-opus-5}"

# Headless auth: long-lived token from .secrets.env. Interactive OAuth
# sessions expire (2026-08-13: this script died on exactly that); the env
# token takes precedence and outlives them.
TOKEN=$(grep -E '^CLAUDE_CODE_OAUTH_TOKEN=' "$REPO/.secrets.env" 2>/dev/null | cut -d= -f2- || true)
[ -n "$TOKEN" ] && export CLAUDE_CODE_OAUTH_TOKEN="$TOKEN"

mkdir -p "$LOGDIR" "$REPO/state/reports"
LOG="$LOGDIR/manager-$(date +%F).log"

# Share the trading lock: never overlap a brain run or healthcheck.
exec 9>"$LOCK"
if ! flock -w 600 9; then
  echo "$(date -Is) SKIP manager: could not acquire lock in 10m" >>"$LOG"
  # The manager is the owner's dead-man switch — if it cannot run, that is
  # itself the most important thing to report. Email needs no lock/broker.
  REPORT="$REPO/state/reports/$(date -u +%F)-BLOCKED.md"
  {
    echo "MANAGER BLOCKED: could not acquire the run lock after 10 minutes."
    echo "A brain run or healthcheck is stuck (or genuinely long-running)."
    echo "No daily review was performed. The system may be stalled — check:"
    echo "  ssh, then: sudo fuser -v /tmp/autoswing-brain.lock"
    echo "  logs in state/brain/logs/, journal in journal/"
    echo "Lock holders at $(date -Is):"
    fuser -v "$LOCK" 2>&1 || true
  } > "$REPORT"
  cd "$REPO" && uv run python scripts/send_report.py \
    --subject "autoswing ALERT: manager blocked — system may be stalled" \
    --body-file "$REPORT" >>"$LOG" 2>&1 \
    || echo "$(date -Is) fallback email FAILED too — report saved: $REPORT" >>"$LOG"
  exit 0
fi

cd "$REPO"
REPORT_FILE="$REPO/state/reports/$(date -u +%F).md"
STAMP=$(mktemp /tmp/autoswing-manager-stamp.XXXXXX)

{
  echo "=== $(date -Is) manager run ==="
  # `|| echo` keeps set -e from silently aborting the script on a claude
  # failure — on 2026-08-13 an expired OAuth session died here with no
  # alert, and the missing report was the only signal.
  timeout --kill-after=60 3600 claude -p "$(cat prompts/manager.md)

TODAY (UTC): $(date -u +%F). Review this trading day." \
    --model "$MODEL" \
    --settings config/manager-settings.json \
    --max-turns 80 \
    --output-format text || echo "claude exited nonzero: $?"
  echo "=== $(date -Is) done ==="
} >>"$LOG" 2>&1

# Dead-man check: a successful review leaves today's report, freshly
# written. Anything else (auth expiry, crash, empty run) must be as loud
# as the lock-blocked path above.
if [ ! -f "$REPORT_FILE" ] || [ ! "$REPORT_FILE" -nt "$STAMP" ]; then
  ALERT="$REPO/state/reports/$(date -u +%F)-FAILED.md"
  {
    echo "MANAGER FAILED: the run finished without writing today's report."
    echo "No daily review was delivered. Log tail ($LOG):"
    echo
    tail -n 15 "$LOG"
  } > "$ALERT"
  uv run python scripts/send_report.py \
    --subject "autoswing ALERT: manager run produced no report" \
    --body-file "$ALERT" >>"$LOG" 2>&1 \
    || echo "$(date -Is) alert email FAILED too — see $ALERT" >>"$LOG"
fi
rm -f "$STAMP"
