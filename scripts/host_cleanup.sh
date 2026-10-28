#!/usr/bin/env bash
# Host housekeeping the owner approved on 2026-10-28 ("option 2"), run by the
# owner session when the disk fills. Allowed as ONE script in
# ~/.claude/settings.json (same pattern as backup_squash.sh); the harness
# denies ad-hoc kills and sudo, so everything this does is listed here and
# nothing else is added without a new owner ruling.
#
# 1. Kill orphaned `ugrep` processes (parent = init). On 2026-10-28 twelve of
#    them from a dead September session were each pinning a deleted 205M
#    journal file: 2.1G "freed" by journalctl --vacuum that df never saw.
#    Only ugrep (Claude Code's search helper) whose parent is PID 1 — a live
#    session's greps have a live parent and are left alone.
# 2. Vacuum the systemd journal to 300M and cap it there (SystemMaxUse) so
#    it cannot grow unbounded again (it was 1.9G).
#
# Idempotent; prints what it did; never touches trading processes, the IB
# gateway, docker, or any file under ~/autoswing.
set -euo pipefail

echo "== before: $(df -h / | awk 'NR==2{print $4" free ("$5")"}')"

# 1. orphaned ugrep
killed=0
for pid in $(pgrep -f '(^|/)ugrep( |$)' || true); do
  ppid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ' || true)
  if [ "$ppid" = "1" ]; then
    started=$(ps -o lstart= -p "$pid" 2>/dev/null || true)
    kill "$pid" 2>/dev/null && { echo "killed orphaned ugrep $pid (started $started)"; killed=$((killed+1)); }
  fi
done
echo "orphaned ugrep killed: $killed"

# 2. journal cap
sudo -n journalctl --vacuum-size=300M >/dev/null 2>&1 || echo "WARN: vacuum failed"
if grep -q '^#SystemMaxUse=$' /etc/systemd/journald.conf; then
  sudo -n sed -i 's/^#SystemMaxUse=$/SystemMaxUse=300M/' /etc/systemd/journald.conf
  sudo -n systemctl restart systemd-journald
  echo "journald: SystemMaxUse=300M set, journald restarted"
elif grep -q '^SystemMaxUse=300M$' /etc/systemd/journald.conf; then
  echo "journald: cap already set"
else
  echo "WARN: journald.conf SystemMaxUse line not in expected form; left alone"
fi
echo "journal usage: $(sudo -n journalctl --disk-usage 2>/dev/null | grep -o '[0-9.]*[MG]' | head -1)"

sleep 2
echo "== after:  $(df -h / | awk 'NR==2{print $4" free ("$5")"}')"
