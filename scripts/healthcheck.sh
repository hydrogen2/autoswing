#!/usr/bin/env bash
# Deterministic hourly healthcheck: exercise every pipeline component
# against live data, no LLM involved. Ramp-up bug squeezer — data feeds
# change shape without warning; this finds out before a trading window does.
set -uo pipefail

REPO=/home/supper-user/autoswing
LOG="$REPO/state/brain/logs/health-$(date +%F).log"
LOCK=/tmp/autoswing-brain.lock
export PATH="$HOME/.local/bin:$PATH"
mkdir -p "$(dirname "$LOG")"

# Share the brain's lock: never talk to the gateway concurrently with a
# brain run (same API client id). Busy = skip, that's fine.
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date -Is) SKIP: brain run in progress" >>"$LOG"
  exit 0
fi

cd "$REPO"
FAILS=()

run_check() { # name, command...
  local name="$1"; shift
  local out code
  # Outer wall-clock cap; the CLI's own SIGALRM watchdog (180s) fires first
  # with a clean JSON error — this is the belt over that suspender.
  out=$(timeout --kill-after=30 300 "$@" 2>&1); code=$?
  # Success keys on the RESULT, not the exit code: yfinance's threaded
  # fetches can exit nonzero after printing a complete ok:true result
  # (market-hours load noise — 6 false FAILs on 2026-07-22). A valid result
  # with a dirty exit is a visible WARN, never a FAIL; timeouts (no output)
  # and real errors (ok:false) still FAIL.
  # Substring test in pure bash — NOT `echo | grep -q`: under pipefail,
  # grep -q's early exit SIGPIPEs echo once output exceeds the 64KB pipe
  # buffer, turning a successful match into a "failure". That was the real
  # cause of the intermittent scan-candidates false FAILs (big earnings-
  # season scans crossed 64KB; quiet-hours reruns didn't).
  if [[ "$out" == *'"ok": true'* ]]; then
    if [ "$code" -ne 0 ]; then
      echo "$(date -Is) WARN $name: ok result but exit $code" >>"$LOG"
    else
      echo "$(date -Is) OK   $name" >>"$LOG"
    fi
  else
    echo "$(date -Is) FAIL $name (exit $code)" >>"$LOG"
    # Head AND tail: the head shows whether the ok-wrapper ever printed,
    # which is the difference between "component broke" and "output mangled".
    echo "$out" | head -6 >>"$LOG"
    echo "  [...]" >>"$LOG"
    echo "$out" | tail -12 >>"$LOG"
    FAILS+=("$name")
  fi
}

run_check "gate-status"       uv run autoswing gate-status
run_check "get-positions"     uv run autoswing get-positions

# Quote is special: an empty-but-successful quote usually means the owner's
# live login holds the market-data seat (single-seat sharing). That's a
# WARN (environmental), not a FAIL (broken component).
QOUT=$(uv run autoswing get-quote AAPL 2>&1)
QSTATE=$(echo "$QOUT" | sed -n '/^{/,$p' | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    if not d.get('ok'): print('fail')
    elif d['result'].get('close') or d['result'].get('last'): print('ok')
    else: print('blackout')
except Exception: print('fail')
")
case "$QSTATE" in
  ok)       echo "$(date -Is) OK   get-quote" >>"$LOG" ;;
  blackout) echo "$(date -Is) WARN get-quote: empty quote — market-data seat likely held by owner's live session" >>"$LOG" ;;
  *)        echo "$(date -Is) FAIL get-quote" >>"$LOG"; echo "$QOUT" | tail -15 >>"$LOG"; FAILS+=("get-quote") ;;
esac
run_check "scan-candidates"   uv run autoswing scan-candidates --days-back 2
run_check "next-earnings"     uv run autoswing next-earnings MSFT
run_check "manage-positions"  uv run autoswing manage-positions
# Orphan-order guard in shadow mode: hourly soak builds the false-positive
# record that gates promotion to enforce (go-live blocker).
run_check "reconcile"         uv run autoswing reconcile
# Gate end-to-end: a dry-run proposal must evaluate cleanly (approval not
# required — outside market hours a rejection is the correct answer).
run_check "propose-dry-run" bash -c 'echo "{\"symbol\":\"XOM\",\"action\":\"BUY\",\"quantity\":10,\"entry_limit\":100.0,\"stop_loss\":97.0,\"take_profit\":112.0,\"rationale\":\"healthcheck\",\"next_earnings_date\":\"none\",\"avg_dollar_volume\":900000000}" | uv run autoswing propose-trade - --dry-run'

if [ ${#FAILS[@]} -gt 0 ]; then
  uv run autoswing journal-note "HEALTHCHECK FAILURE: ${FAILS[*]} — see $LOG. Brain: if a trading window hits this broken component, stand down and note it." >>"$LOG" 2>&1
  exit 1
fi
