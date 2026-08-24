#!/usr/bin/env bash
# Daily manager/dev review run. Invoked by cron after the close.
set -euo pipefail

REPO=/home/supper-user/autoswing
LOGDIR="$REPO/state/brain/logs"
LOCK=/tmp/autoswing-brain.lock
export PATH="$HOME/.local/bin:$PATH"

# Models are PINNED, not inherited (see brain_run.sh for the full reasoning).
# Sonnet 5 -> Opus 5 on 2026-08-20, -> Fable 5 on 2026-08-24. Quota
# exhaustion falls back to the secondary model once rather than losing the
# review entirely — 2026-08-18 lost a whole day's report to a session limit.
MODEL_PRIMARY="${AUTOSWING_MODEL:-claude-fable-5}"
MODEL_FALLBACK="${AUTOSWING_MODEL_FALLBACK:-claude-opus-5}"
QUOTA_RE='(session|usage|weekly|monthly) limit|out of (quota|credit)|quota (exceeded|exhausted)'


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

PROMPT="$(cat prompts/manager.md)

TODAY (UTC): $(date -u +%F). Review this trading day."

run_manager() {   # $1 = model; echoes output, returns claude's exit status
  local rc=0
  timeout --kill-after=60 3600 claude -p "$PROMPT" \
    --model "$1" \
    --settings config/manager-settings.json \
    --max-turns 80 \
    --output-format text >"$OUT" 2>&1 || rc=$?
  cat "$OUT"
  return $rc
}

OUT=$(mktemp /tmp/autoswing-manager-out.XXXXXX)
ACTIVE_MODEL="$MODEL_PRIMARY"
{
  echo "=== $(date -Is) manager run (model $ACTIVE_MODEL) ==="
  # Never let a claude failure abort the script under set -e — on 2026-08-13
  # an expired OAuth session died here with no alert, and the missing report
  # was the only signal. Quota exhaustion additionally retries on the
  # fallback model, since the primary will not recover within this run.
  RC=0
  run_manager "$ACTIVE_MODEL" || RC=$?
  if [ "$RC" -ne 0 ]; then
    echo "$(date -Is) manager failed on $ACTIVE_MODEL (exit $RC)"
    if grep -qiE "$QUOTA_RE" "$OUT"; then
      ACTIVE_MODEL="$MODEL_FALLBACK"
      echo "$(date -Is) FALLBACK: $MODEL_PRIMARY out of quota -> retrying on $ACTIVE_MODEL"
      RC=0
      run_manager "$ACTIVE_MODEL" || RC=$?
      [ "$RC" -ne 0 ] && echo "$(date -Is) fallback model also failed (exit $RC)"
    fi
  fi
  echo "=== $(date -Is) done (model=$ACTIVE_MODEL) ==="
} >>"$LOG" 2>&1
rm -f "$OUT"

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
