#!/usr/bin/env bash
# Prove the backup is restorable. Restores real files and compares hashes
# against the live vault. Secrets are sourced, never printed.
set -uo pipefail
set -a; . "$HOME/.config/restic/b2-box.env"; set +a
VAULT="$HOME/vault"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "=== snapshots in the repository ==="
restic snapshots --compact || exit 1
echo
echo "=== repository size ==="
restic stats latest --mode raw-data 2>/dev/null | sed 's/^/  /'
echo
echo "=== restoring a sample, including files that once went missing ==="
restic restore latest --target "$TMP" \
  --include "$VAULT/<SAMPLE_VAULT_FILE_1>" \
  --include "$VAULT/<SAMPLE_VAULT_FILE_2>" \
  --include "$VAULT/<SAMPLE_VAULT_FILE_3>" \
  --include "$VAULT/_system/vps-vault-host.md" \
  --include "$VAULT/<SAMPLE_VAULT_FILE_4>" \
  >/dev/null 2>&1 || { echo "RESTORE FAILED"; exit 1; }

echo
echo "=== byte-for-byte comparison against the live vault ==="
ok=0; bad=0
while IFS= read -r -d '' f; do
  rel="${f#$TMP}"
  live="$rel"
  if [ -f "$live" ]; then
    if [ "$(sha256sum "$f" | cut -d" " -f1)" = "$(sha256sum "$live" | cut -d" " -f1)" ]; then
      ok=$((ok+1)); printf "  MATCH   %s\n" "$(basename "$rel")"
    else
      bad=$((bad+1)); printf "  DIFFERS %s\n" "$(basename "$rel")"
    fi
  else
    bad=$((bad+1)); printf "  MISSING IN VAULT %s\n" "$rel"
  fi
done < <(find "$TMP" -type f -print0)
echo
echo "  identical: $ok   mismatched: $bad"
[ "$bad" -eq 0 ] && echo "  RESTORE TEST PASSED" || echo "  RESTORE TEST FAILED"
