#!/usr/bin/env bash
# Nightly offsite backup: encrypt the irreplaceables and push to a private
# GitHub repo (hydrogen2/autoswing-backup). Runs from cron at 05:10 UTC.
#
# Contents: state/ (gate anchor, benchmark track record, reconciler soak),
# journal/ (full audit trail), docker/.env + .secrets.env (credentials),
# and the Claude project dir (memory + conversation transcripts).
#
# RESTORE (on any machine):
#   gh repo clone hydrogen2/autoswing-backup
#   openssl enc -d -aes-256-cbc -pbkdf2 -in autoswing-backup-<date>.tar.gz.enc \
#     | tar xzf -        # passphrase = BACKUP_PASS (in your password manager)
#   Then place files per MIGRATION.md's asset table ("claude-project" goes to
#   ~/.claude/projects/<repo-path-with-dashes>/).
set -euo pipefail

REPO=/home/supper-user/autoswing
BACKUP_REPO=/home/supper-user/autoswing-backup
PROJDIR=/home/supper-user/.claude/projects/-home-supper-user-autoswing
LOCK=/tmp/autoswing-brain.lock
LOG="$REPO/state/brain/logs/backup.log"
export PATH="/home/supper-user/.local/bin:$PATH"

log() { echo "$(date -Is) $*" >>"$LOG"; }

alert() {  # subject-suffix, body
  # The body is written to RAM (/dev/shm), not the disk: on 2026-10-10/11
  # the disk was full, the body file came out 0 bytes, and the owner got two
  # BLANK alert emails about a failure caused by the disk being full. The
  # on-disk copy under state/reports is kept for the record but is allowed
  # to fail. The subject carries the reason, so even an empty body says why.
  local body="/dev/shm/autoswing-backup-alert-$(date -u +%F).md"
  local keep="$REPO/state/reports/backup-failure-$(date -u +%F).md"
  printf '%s\n\n%s\n' "$2" "$(df -h / 2>/dev/null | tail -1)" > "$body" 2>/dev/null \
    || body="/dev/null"
  cp "$body" "$keep" 2>/dev/null || log "could not keep alert body on disk"
  (cd "$REPO" && uv run python scripts/send_report.py \
     --subject "autoswing ALERT: nightly backup $1" --body-file "$body") \
     >>"$LOG" 2>&1 || log "alert email failed too"
}

trap 'log "FAILED at line $LINENO"; alert "FAILED at line $LINENO ($(df -h / 2>/dev/null | awk "NR==2{print \$4}") free)" "Nightly backup failed at $(date -Is), line $LINENO. Check state/brain/logs/backup.log on the server."' ERR

BACKUP_PASS=$(grep '^BACKUP_PASS=' "$REPO/.secrets.env" | cut -d= -f2-)
[ -n "$BACKUP_PASS" ] || { log "no BACKUP_PASS in .secrets.env"; alert "MISCONFIGURED" "BACKUP_PASS missing from .secrets.env — backups cannot run."; exit 1; }
export BACKUP_PASS

# Disk preflight. A backup needs room for the artifact (~55M), its git
# object (same again) and the decrypt-verify pass; below this floor the run
# would fail part-way and, worse, leave the trading-hours journal/state
# writes with nothing to write into. Refuse loudly instead.
MIN_FREE_MB=600
FREE_MB=$(df -Pm / | awk 'NR==2{print $4}')
if [ "${FREE_MB:-0}" -lt "$MIN_FREE_MB" ]; then
  log "SKIP: only ${FREE_MB}M free on / (floor ${MIN_FREE_MB}M)"
  alert "SKIPPED: disk nearly full (${FREE_MB}M free)" \
    "Backup not attempted: only ${FREE_MB}M free on / (floor ${MIN_FREE_MB}M). Free space on the server before the next trading session — journal and state writes are at risk."
  exit 1
fi

# Quiet hours; still take the lock briefly so we never snapshot mid-write.
exec 9>"$LOCK"
flock -w 300 9 || { log "SKIP: lock busy 5m"; alert "SKIPPED (lock busy)" "Backup could not acquire the run lock for 5 minutes — system may be stalled."; exit 1; }

OUT="$BACKUP_REPO/autoswing-backup-$(date -u +%F).tar.gz.enc"
tar czf - \
  -C "$REPO" state journal docker/.env .secrets.env \
  -C "$(dirname "$PROJDIR")" \
  --transform 's|^\./-home-supper-user-autoswing|claude-project|' \
  "./$(basename "$PROJDIR")" \
  | openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:BACKUP_PASS -out "$OUT"
flock -u 9

# Prove the artifact is decryptable before trusting it.
openssl enc -d -aes-256-cbc -pbkdf2 -pass env:BACKUP_PASS -in "$OUT" \
  | tar tzf - >/dev/null

cd "$BACKUP_REPO"
# Retention (owner ruling 2026-10-14): keep the newest KEEP_DAYS artifacts.
# Each artifact is a FULL snapshot (journal/ and state/ are cumulative), so an
# older one adds nothing the newest does not already hold. 90 days of them
# filled the disk on 10-10. Note this only bounds the working tree: git
# history still holds every blob ever committed, so the repo's .git grows
# ~55M/day until its history is squashed (an owner-run step; see the
# 2026-10-14 report).
KEEP_DAYS=14
ls -1 "$BACKUP_REPO"/autoswing-backup-*.tar.gz.enc 2>/dev/null | sort | head -n -"$KEEP_DAYS" \
  | while read -r old; do git rm -q --cached "$old" 2>/dev/null || true; rm -f "$old"; done
git add -A
git commit -q -m "backup $(date -u +%F)" 2>/dev/null || { log "nothing new to commit"; exit 0; }
git push -q
log "OK $(basename "$OUT") ($(du -h "$OUT" | cut -f1)); kept $(ls -1 "$BACKUP_REPO"/autoswing-backup-*.tar.gz.enc | wc -l) artifacts, .git $(du -sm "$BACKUP_REPO/.git" | cut -f1)M"
