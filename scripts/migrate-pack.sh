#!/usr/bin/env bash
# Bundle everything git does NOT carry into one encrypted tarball:
# credentials, gate/reconciler state, journal history, benchmark record,
# and the Claude Code project memory. Run on the OLD machine at migration
# time (state changes daily — pack fresh, not in advance).
#
# Usage: scripts/migrate-pack.sh [output-dir]   (default: $HOME)
set -euo pipefail

REPO=/home/supper-user/autoswing
MEMDIR=/home/supper-user/.claude/projects/-home-supper-user-autoswing
OUT="${1:-$HOME}/autoswing-migrate-$(date +%Y%m%d).tar.gz.enc"

cd "$REPO"
for f in docker/.env .secrets.env state journal; do
  [ -e "$f" ] || { echo "MISSING: $f — aborting"; exit 1; }
done
[ -d "$MEMDIR/memory" ] || { echo "MISSING: $MEMDIR/memory — aborting"; exit 1; }

# Passphrase: interactive prompt by default; set AUTOSWING_PACK_PASS to
# run non-interactively.
PASSARG=()
[ -n "${AUTOSWING_PACK_PASS:-}" ] && PASSARG=(-pass env:AUTOSWING_PACK_PASS)

echo "Packing secrets + state + journal + claude memory..."
tar czf - \
  -C "$REPO" docker/.env .secrets.env state journal \
  -C "$MEMDIR" memory \
  | openssl enc -aes-256-cbc -pbkdf2 -salt "${PASSARG[@]}" -out "$OUT"

chmod 600 "$OUT"
echo "Wrote: $OUT"
echo "Carry this file to the new machine (scp), then run scripts/migrate-unpack.sh there."
echo "REMEMBER THE PASSPHRASE — it is not stored anywhere."
