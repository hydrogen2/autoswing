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
  local body="$REPO/state/reports/backup-failure-$(date -u +%F).md"
  printf '%s\n' "$2" > "$body"
  (cd "$REPO" && uv run python scripts/send_report.py \
     --subject "autoswing ALERT: nightly backup $1" --body-file "$body") \
     >>"$LOG" 2>&1 || log "alert email failed too"
}

trap 'log "FAILED at line $LINENO"; alert "FAILED" "Nightly backup failed at $(date -Is), line $LINENO. Check state/brain/logs/backup.log on the server."' ERR

BACKUP_PASS=$(grep '^BACKUP_PASS=' "$REPO/.secrets.env" | cut -d= -f2-)
[ -n "$BACKUP_PASS" ] || { log "no BACKUP_PASS in .secrets.env"; alert "MISCONFIGURED" "BACKUP_PASS missing from .secrets.env — backups cannot run."; exit 1; }
export BACKUP_PASS

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
git add -A
git commit -q -m "backup $(date -u +%F)" 2>/dev/null || { log "nothing new to commit"; exit 0; }
git push -q
log "OK $(basename "$OUT") ($(du -h "$OUT" | cut -f1))"
