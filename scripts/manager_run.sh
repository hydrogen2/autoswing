#!/usr/bin/env bash
# Daily manager/dev review run. Invoked by cron after the close.
set -euo pipefail

REPO=/home/supper-user/autoswing
LOGDIR="$REPO/state/brain/logs"
LOCK=/tmp/autoswing-brain.lock
export PATH="$HOME/.local/bin:$PATH"

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
{
  echo "=== $(date -Is) manager run ==="
  timeout --kill-after=60 3600 claude -p "$(cat prompts/manager.md)

TODAY (UTC): $(date -u +%F). Review this trading day." \
    --settings config/manager-settings.json \
    --max-turns 80 \
    --output-format text
  echo "=== $(date -Is) done (exit $?) ==="
} >>"$LOG" 2>&1
