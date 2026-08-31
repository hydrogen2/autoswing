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
  LAST_OUT="$out"
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
# recent-fills here is not just a liveness probe: the hourly cadence is the
# only thing watching the 15:30-16:00 gap. Fills into the closing auction
# land after preclose has run, and recent-fills is today-only, so the next
# morning returns [] and the fill is invisible to every brain window (EL
# 2026-08-20, 50 @ 96.52, unnarrated for a day). The 16:45 ET run captures
# it into the journal, where the nightly review picks it up.
run_check "recent-fills"      uv run autoswing recent-fills

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
run_check "shadow-status"     uv run autoswing shadow-status
run_check "forecast-stats"    uv run autoswing forecast-stats
# Gate end-to-end: a dry-run proposal must evaluate cleanly (approval not
# required — outside market hours a rejection is the correct answer).
run_check "propose-dry-run" bash -c 'echo "{\"symbol\":\"XOM\",\"action\":\"BUY\",\"quantity\":10,\"entry_limit\":100.0,\"stop_loss\":97.0,\"take_profit\":112.0,\"rationale\":\"healthcheck\",\"next_earnings_date\":\"none\",\"avg_dollar_volume\":900000000}" | uv run autoswing propose-trade - --dry-run'

# Portfolio-halt canary (2026-07-23: exposure double-count silently blocked
# ALL entries for 3 days while this check logged OK). A dry-run refused by a
# PORTFOLIO-level rule means no candidate whatsoever can pass — that is a
# trading outage, not a normal rejection. Checked during regular hours only
# (ET-computed, DST-safe); off-hours rejections are correct behaviour.
# JSON is parsed, not substring-matched: rules serialize multi-line.
ET_MIN=$((10#$(TZ=America/New_York date +%H) * 60 + 10#$(TZ=America/New_York date +%M)))
if [ "$ET_MIN" -ge 570 ] && [ "$ET_MIN" -lt 960 ]; then
  BLOCKED=$(printf '%s' "$LAST_OUT" | sed -n '/^{/,$p' | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    rules = d['result']['decision']['rules']
    bad = [r['rule'] for r in rules if not r['passed'] and r['rule'] in
           ('kill_switch', 'daily_loss_halt', 'max_gross_exposure', 'max_open_positions')]
    print(' '.join(bad))
except Exception:
    pass")
  if [ -n "$BLOCKED" ]; then
    echo "$(date -Is) WARN propose-dry-run: canary blocked by $BLOCKED — NO entry can pass (portfolio-level halt)" >>"$LOG"
    uv run autoswing journal-note "HEALTHCHECK portfolio-halt canary: dry-run blocked by $BLOCKED during RTH — no entry can pass. If unexpected, this is a silent trading outage (cf. 2026-07-23)." >>"$LOG" 2>&1 || true
  fi
fi

# Self-heal the one failure we have a proven, bounded remedy for: IBKR's
# paper-trading disclaimer dialog (error 10141) opens inside the gateway
# container on some re-logins and blocks EVERY API call until the container
# is restarted. Seen 2026-08-24 and again 2026-08-30 (that one on a Sunday,
# invisible for 14h). A restart is the only thing that clears it; accepting
# the disclaimer in the app does not, and the obvious IBC setting
# (AcceptNonBrokerageAccountWarning) is already enabled and does not prevent it.
#
# Deliberately narrow — this is an automated admin action on a live account:
#   * fires ONLY on the 10141 signature, never on generic failures;
#   * at most once per 6h (stamp file), so a persistent fault cannot become
#     a restart loop that repeatedly drops the API mid-session;
#   * always journals before and after, and re-verifies with gate-status;
#   * never touches positions or orders — bracket legs live at IBKR and have
#     been verified intact across both manual restarts.
RESTART_STAMP=/tmp/autoswing-gw-restart.stamp
if [ ${#FAILS[@]} -gt 0 ] && grep -q "10141" "$LOG" 2>/dev/null; then
  if [ -f "$RESTART_STAMP" ] && [ $(( $(date +%s) - $(stat -c %Y "$RESTART_STAMP") )) -lt 21600 ]; then
    echo "$(date -Is) 10141 seen but a gateway restart already ran within 6h — not retrying" >>"$LOG"
    uv run autoswing journal-note "HEALTHCHECK: error 10141 persists after a gateway restart within the last 6h. Auto-remediation is NOT retrying — this needs a human. The paper-disclaimer dialog may require accepting in the IBKR Client Portal as the PAPER user." >>"$LOG" 2>&1 || true
  else
    date > "$RESTART_STAMP"
    echo "$(date -Is) AUTO-REMEDIATE: error 10141 detected, restarting ib-gateway" >>"$LOG"
    uv run autoswing journal-note "HEALTHCHECK AUTO-REMEDIATE: error 10141 (paper-disclaimer dialog) blocked all broker calls; restarting docker-ib-gateway-1. Bracket legs live at IBKR and are unaffected by a container restart (verified 08-25 and 08-31)." >>"$LOG" 2>&1 || true
    docker restart docker-ib-gateway-1 >>"$LOG" 2>&1 || echo "$(date -Is) gateway restart command FAILED" >>"$LOG"
    sleep 90
    if uv run autoswing gate-status >>"$LOG" 2>&1; then
      echo "$(date -Is) AUTO-REMEDIATE OK: API responding after restart" >>"$LOG"
      uv run autoswing journal-note "HEALTHCHECK AUTO-REMEDIATE succeeded: API responding after gateway restart. Next window should verify positions and bracket legs before trading." >>"$LOG" 2>&1 || true
    else
      echo "$(date -Is) AUTO-REMEDIATE FAILED: API still down after restart" >>"$LOG"
      uv run autoswing journal-note "HEALTHCHECK AUTO-REMEDIATE FAILED: API still down after a gateway restart. Human needed — see the 08-24 incident memory for the escalation path." >>"$LOG" 2>&1 || true
    fi
  fi
fi

if [ ${#FAILS[@]} -gt 0 ]; then
  uv run autoswing journal-note "HEALTHCHECK FAILURE: ${FAILS[*]} — see $LOG. Brain: if a trading window hits this broken component, stand down and note it." >>"$LOG" 2>&1
  exit 1
fi
