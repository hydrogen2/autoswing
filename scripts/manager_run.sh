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
  exit 0
fi

cd "$REPO"
{
  echo "=== $(date -Is) manager run ==="
  claude -p "$(cat prompts/manager.md)

TODAY (UTC): $(date -u +%F). Review this trading day." \
    --settings config/manager-settings.json \
    --max-turns 80 \
    --output-format text
  echo "=== $(date -Is) done (exit $?) ==="
} >>"$LOG" 2>&1
