#!/usr/bin/env bash
# Run one brain window: brain_run.sh {premarket|entry|midday|preclose}
# Invoked by cron; safe to run by hand.
set -euo pipefail

WINDOW="${1:?usage: brain_run.sh <premarket|entry|midday|preclose>}"
REPO=/home/supper-user/autoswing
LOGDIR="$REPO/state/brain/logs"
LOCK=/tmp/autoswing-brain.lock
export PATH="$HOME/.local/bin:$PATH"

mkdir -p "$LOGDIR"
LOG="$LOGDIR/$(date +%F)-$WINDOW.log"

# ET-time guard: cron fires each window at both EDT and EST UTC offsets;
# whichever lands outside the window's ET target self-skips. Bypass with
# a second arg of --force for manual runs.
if [ "${2:-}" != "--force" ]; then
  case "$WINDOW" in
    premarket) TARGET=480 ;;   # 08:00 ET, minutes since midnight
    entry)     TARGET=600 ;;   # 10:00
    midday)    TARGET=750 ;;   # 12:30
    preclose)  TARGET=930 ;;   # 15:30
    *) echo "unknown window $WINDOW" >>"$LOG"; exit 1 ;;
  esac
  ET_MIN=$((10#$(TZ=America/New_York date +%H) * 60 + 10#$(TZ=America/New_York date +%M)))
  ET_DOW=$(TZ=America/New_York date +%u)
  DIFF=$((ET_MIN - TARGET)); DIFF=${DIFF#-}
  if [ "$ET_DOW" -gt 5 ] || [ "$DIFF" -gt 40 ]; then
    echo "$(date -Is) SKIP $WINDOW: ET time guard (dow=$ET_DOW, off-target ${DIFF}m)" >>"$LOG"
    exit 0
  fi
fi

# Never run two brains at once (a hung run must not overlap the next window).
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date -Is) SKIP $WINDOW: another brain run holds the lock" >>"$LOG"
  exit 0
fi

cd "$REPO"
{
  echo "=== $(date -Is) brain window: $WINDOW ==="
  claude -p "$(cat prompts/brain.md)

TODAY'S RUN WINDOW: $WINDOW" \
    --settings config/brain-settings.json \
    --max-turns 60 \
    --output-format text
  echo "=== $(date -Is) done (exit $?) ==="
} >>"$LOG" 2>&1
