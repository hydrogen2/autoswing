#!/usr/bin/env bash
# Restore a migrate-pack tarball on the NEW machine.
# Run from the freshly cloned repo root: scripts/migrate-unpack.sh <tarball>
# Restores repo-local files in place and Claude memory to this machine's
# project directory (path computed from the repo location).
set -euo pipefail

TARBALL="${1:?usage: migrate-unpack.sh <autoswing-migrate-*.tar.gz.enc>}"
REPO="$(pwd)"
[ -f "$REPO/pyproject.toml" ] || { echo "run from the autoswing repo root"; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

PASSARG=()
[ -n "${AUTOSWING_PACK_PASS:-}" ] && PASSARG=(-pass env:AUTOSWING_PACK_PASS)

echo "Decrypting..."
openssl enc -d -aes-256-cbc -pbkdf2 "${PASSARG[@]}" -in "$TARBALL" | tar xzf - -C "$TMP"

cp "$TMP/docker/.env" "$REPO/docker/.env"
cp "$TMP/.secrets.env" "$REPO/.secrets.env"
cp -r "$TMP/state" "$REPO/"
cp -r "$TMP/journal" "$REPO/"
chmod 600 "$REPO/docker/.env" "$REPO/.secrets.env"

# Claude Code memory: project dir name is the repo path with / -> -
PROJDIR="$HOME/.claude/projects/$(echo "$REPO" | tr '/' '-')"
mkdir -p "$PROJDIR"
cp -r "$TMP/memory" "$PROJDIR/"
echo "Memory restored to $PROJDIR/memory"

echo "Restored: docker/.env .secrets.env state/ journal/ + claude memory"
echo "Next: follow MIGRATION.md from step 5 (uv sync, tests, gateway, cron)."
