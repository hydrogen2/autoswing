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
  local out
  if out=$("$@" 2>&1) && echo "$out" | grep -q '"ok": true'; then
    echo "$(date -Is) OK   $name" >>"$LOG"
  else
    echo "$(date -Is) FAIL $name" >>"$LOG"
    echo "$out" | tail -15 >>"$LOG"
    FAILS+=("$name")
  fi
}

run_check "gate-status"       uv run autoswing gate-status
run_check "get-positions"     uv run autoswing get-positions
run_check "get-quote"         uv run autoswing get-quote AAPL
run_check "scan-candidates"   uv run autoswing scan-candidates --days-back 2
run_check "next-earnings"     uv run autoswing next-earnings MSFT
run_check "manage-positions"  uv run autoswing manage-positions
# Gate end-to-end: a dry-run proposal must evaluate cleanly (approval not
# required — outside market hours a rejection is the correct answer).
run_check "propose-dry-run" bash -c 'echo "{\"symbol\":\"XOM\",\"action\":\"BUY\",\"quantity\":10,\"entry_limit\":100.0,\"stop_loss\":97.0,\"take_profit\":112.0,\"rationale\":\"healthcheck\",\"next_earnings_date\":\"none\",\"avg_dollar_volume\":900000000}" | uv run autoswing propose-trade - --dry-run'

if [ ${#FAILS[@]} -gt 0 ]; then
  uv run autoswing journal-note "HEALTHCHECK FAILURE: ${FAILS[*]} — see $LOG. Brain: if a trading window hits this broken component, stand down and note it." >>"$LOG" 2>&1
  exit 1
fi
