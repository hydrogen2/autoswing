#!/usr/bin/env bash
# Squash the offsite backup repo's history down to the newest KEEP artifacts.
#
# Owner ruling 2026-10-14. Each nightly artifact is a FULL snapshot (journal/
# and state/ are cumulative), so older ones add nothing; but git keeps every
# blob ever committed, and 90 days of 55M artifacts filled the disk on 10-10.
# backup.sh prunes the working tree nightly; this rewrites history so .git
# shrinks too, and backup.sh calls it when .git grows past SQUASH_AT_MB.
#
# DESTRUCTIVE on purpose: force-pushes a single-commit history. Only the newest
# KEEP artifacts survive, locally and on GitHub. Verifies the newest artifact
# decrypts before touching anything.
set -euo pipefail

REPO=/home/supper-user/autoswing
BACKUP_REPO=/home/supper-user/autoswing-backup
KEEP="${KEEP:-14}"
LOG="$REPO/state/brain/logs/backup.log"
log() { echo "$(date -Is) squash: $*" >>"$LOG"; echo "$*"; }

cd "$BACKUP_REPO"
mapfile -t ALL < <(ls -1 autoswing-backup-*.tar.gz.enc 2>/dev/null | sort)
[ "${#ALL[@]}" -gt 0 ] || { log "no artifacts found, refusing"; exit 1; }
NEWEST="${ALL[-1]}"

# Prove the artifact we are about to make the sole lineage is good.
BACKUP_PASS=$(grep '^BACKUP_PASS=' "$REPO/.secrets.env" | cut -d= -f2-)
[ -n "$BACKUP_PASS" ] || { log "no BACKUP_PASS, refusing"; exit 1; }
export BACKUP_PASS
openssl enc -d -aes-256-cbc -pbkdf2 -pass env:BACKUP_PASS -in "$NEWEST" \
  | tar tzf - >/dev/null || { log "$NEWEST does not decrypt, refusing"; exit 1; }

BEFORE_GIT=$(du -sm .git | cut -f1)
BRANCH=$(git rev-parse --abbrev-ref HEAD)
[ "$BRANCH" != "HEAD" ] || BRANCH=main

git checkout -q --orphan squash-tmp
git rm -rfq --cached .
if [ "${#ALL[@]}" -gt "$KEEP" ]; then
  printf '%s\n' "${ALL[@]:0:${#ALL[@]}-KEEP}" | xargs rm -f
fi
git add -A
git commit -q -m "backup $(date -u +%F) — history squashed $(date -u +%F): newest $KEEP artifacts (each a full snapshot)"
# `branch -D` drops the old branch's upstream config with it; -u restores it,
# otherwise the next nightly `git push` fails with "no upstream branch"
# (that is exactly what happened on 2026-10-15, the morning after the first
# squash).
git branch -D -q "$BRANCH"
git branch -m "$BRANCH"
git push -q --force -u origin "$BRANCH"
git reflog expire --expire=now --all
rm -f .git/objects/*/tmp_obj_* 2>/dev/null || true
git gc -q --prune=now
AFTER_GIT=$(du -sm .git | cut -f1)
log "OK kept $(ls -1 autoswing-backup-*.tar.gz.enc | wc -l) artifacts; .git ${BEFORE_GIT}M -> ${AFTER_GIT}M; $(df -h / | awk 'NR==2{print $4}') free on /"
