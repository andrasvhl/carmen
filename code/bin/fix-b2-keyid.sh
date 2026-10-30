#!/usr/bin/env bash
# Repair just the B2 keyID, keeping the existing passphrase and secret.
# Safe to run repeatedly. Nothing is echoed.
set -uo pipefail
ENV_FILE="$HOME/.config/restic/b2-box.env"
[ -r "$ENV_FILE" ] || { echo "no env file at $ENV_FILE — run setup-b2.sh instead"; exit 1; }

echo
echo "Get the keyID from: secure.backblaze.com > Application Keys"
echo "It is the 'keyID' column, NOT the key name and NOT the bucket name."
echo "It is a hex string. If it looks like '<B2_KEY_NAME>', that is the wrong field."
echo
read -rs -p "keyID: " NEWID; echo
[ -n "$NEWID" ] || { echo "ABORT: empty."; exit 1; }

case "$NEWID" in
  *[!0-9a-fA-F]*) echo "WARNING: that is not pure hex, which a B2 keyID normally is."
                  read -r -p "Use it anyway? [y/N] " yn
                  [ "$yn" = "y" ] || { echo "Aborted, nothing changed."; exit 1; } ;;
esac

cp -p "$ENV_FILE" "$ENV_FILE.bak"
umask 077
python3 - "$ENV_FILE" "$NEWID" <<'PY'
import sys
p, newid = sys.argv[1], sys.argv[2]
out=[]
for line in open(p, encoding="utf-8"):
    out.append(f"B2_ACCOUNT_ID={newid}\n" if line.startswith("B2_ACCOUNT_ID=") else line)
open(p, "w", encoding="utf-8").writelines(out)
PY
chmod 600 "$ENV_FILE"
unset NEWID

echo "--- re-testing auth against B2 ---"
set -a; . "$ENV_FILE"; set +a
code=$(curl -s -o /dev/null -w "%{http_code}" -u "${B2_ACCOUNT_ID}:${B2_ACCOUNT_KEY}" \
  https://api.backblazeb2.com/b2api/v3/b2_authorize_account)
if [ "$code" != "200" ]; then
  echo "STILL FAILING (HTTP $code). Previous file kept at $ENV_FILE.bak"
  echo "If the secret itself is wrong, create a NEW application key and re-run setup-b2.sh."
  exit 1
fi
rm -f "$ENV_FILE.bak"
echo "AUTH OK."
echo
echo "--- initialising repository and running the first backup (~2.6 GB) ---"
restic snapshots >/dev/null 2>&1 || restic init || { echo "init failed"; exit 1; }
exec "$HOME/automation/bin/backup-vault.sh"
