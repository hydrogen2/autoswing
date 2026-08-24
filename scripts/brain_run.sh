#!/usr/bin/env bash
# Run one brain window: brain_run.sh {premarket|entry|midday|preclose}
# Invoked by cron; safe to run by hand.
set -euo pipefail

WINDOW="${1:?usage: brain_run.sh <premarket|entry|midday|preclose>}"
REPO=/home/supper-user/autoswing
LOGDIR="$REPO/state/brain/logs"
LOCK=/tmp/autoswing-brain.lock
export PATH="$HOME/.local/bin:$PATH"

# Models are PINNED, not inherited. Two reasons: (1) an account-level default
# change must never silently alter trading behaviour mid-experiment — the
# lessons / wide-ledger / forecast series are only comparable within a model
# regime; (2) the owner's interactive /model choice does not reach cron.
# Sonnet 5 -> Opus 5 on 2026-08-20, -> Fable 5 on 2026-08-24 (owner reserves
# Fable quota for autoswing). See docs/model-history.md.
#
# FALLBACK: quota exhaustion is not transient — retrying the same model just
# burns the window (2026-08-18: another project drained the session limit and
# killed midday). On a quota/limit error we switch to the fallback model once
# and retry immediately, because a different model has separate headroom.
MODEL_PRIMARY="${AUTOSWING_MODEL:-claude-fable-5}"
MODEL_FALLBACK="${AUTOSWING_MODEL_FALLBACK:-claude-opus-5}"
# Matches "You've hit your session limit", usage/weekly limits, and quota
# wording. Deliberately NOT matching 429/"rate limit"/"overloaded": those are
# transient and the existing same-model retry is the right response.
QUOTA_RE='(session|usage|weekly|monthly) limit|out of (quota|credit)|quota (exceeded|exhausted)'

# Headless auth: long-lived token from .secrets.env. Interactive OAuth
# sessions expire (2026-08-13: manager run died on exactly that); the env
# token takes precedence and outlives them.
TOKEN=$(grep -E '^CLAUDE_CODE_OAUTH_TOKEN=' "$REPO/.secrets.env" 2>/dev/null | cut -d= -f2- || true)
[ -n "$TOKEN" ] && export CLAUDE_CODE_OAUTH_TOKEN="$TOKEN"

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
# Lets window-scoped commands (benchmark-mark: preclose-only) self-enforce.
export AUTOSWING_WINDOW="$WINDOW"
OK=0
ACTIVE_MODEL="$MODEL_PRIMARY"
FELL_BACK=0
{
  echo "=== $(date -Is) brain window: $WINDOW (model $ACTIVE_MODEL) ==="
  # Transient API failures get up to 3 attempts (2026-08-13: a 1-second
  # "529 Overloaded" killed the entry window via set -e — no retry, no
  # alert). Only FAST failures retry: a run that died after >=120s may
  # have already done real work, and a retry chain would hold the shared
  # lock into the hourly healthcheck or the next window. Wall-clock cap
  # per attempt: a hung run must never hold the lock into later windows.
  for attempt in 1 2 3; do
    ATTEMPT_START=$SECONDS
    OUT=$(mktemp /tmp/autoswing-window-out.XXXXXX)
    RC=0
    timeout --kill-after=60 2400 claude -p "$(cat prompts/brain.md)

TODAY'S RUN WINDOW: $WINDOW" \
      --model "$ACTIVE_MODEL" \
      --settings config/brain-settings.json \
      --max-turns 60 \
      --output-format text >"$OUT" 2>&1 || RC=$?
    cat "$OUT"
    if [ "$RC" -eq 0 ]; then
      OK=1
      rm -f "$OUT"
      break
    fi
    DUR=$((SECONDS - ATTEMPT_START))
    echo "$(date -Is) attempt $attempt failed on $ACTIVE_MODEL (exit $RC after ${DUR}s)"
    if [ "$FELL_BACK" -eq 0 ] && grep -qiE "$QUOTA_RE" "$OUT"; then
      FELL_BACK=1
      ACTIVE_MODEL="$MODEL_FALLBACK"
      echo "$(date -Is) FALLBACK: $MODEL_PRIMARY out of quota -> retrying on $ACTIVE_MODEL"
      rm -f "$OUT"
      continue   # no sleep: quota will not clear, but the other model has its own
    fi
    rm -f "$OUT"
    if [ "$DUR" -ge 120 ] || [ "$attempt" -eq 3 ]; then
      break
    fi
    sleep 300
  done
  echo "=== $(date -Is) done (ok=$OK, model=$ACTIVE_MODEL, fell_back=$FELL_BACK) ==="
} >>"$LOG" 2>&1

# Dead window = lost trading decisions; must be as loud as a manager miss.
if [ "$OK" -ne 1 ]; then
  ALERT=$(mktemp /tmp/autoswing-window-alert.XXXXXX)
  {
    echo "BRAIN WINDOW FAILED: $WINDOW on $(date -u +%F) — attempts exhausted."
    echo "This window's checklist never completed. Log tail ($LOG):"
    echo
    tail -n 15 "$LOG"
  } > "$ALERT"
  uv run python scripts/send_report.py \
    --subject "autoswing ALERT: $WINDOW brain window failed" \
    --body-file "$ALERT" >>"$LOG" 2>&1 \
    || echo "$(date -Is) alert email FAILED too" >>"$LOG"
  rm -f "$ALERT"
fi
