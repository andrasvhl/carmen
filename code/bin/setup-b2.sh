#!/usr/bin/env bash
# Interactive one-time setup for the vault's Backblaze B2 restic repository.
# Run this yourself in your own terminal. Secrets are read with hidden input and
# written straight to a chmod-600 file; nothing is echoed to logs.

set -uo pipefail
ENV_FILE="$HOME/.config/restic/b2-box.env"
mkdir -p "$(dirname "$ENV_FILE")"

echo
echo "=============================================="
echo " Backblaze B2 setup for the vault backup"
echo "=============================================="
echo
echo "You need, from secure.backblaze.com:"
echo "  1. your bucket name        (B2 Cloud Storage > Buckets)"
echo "  2. the keyID               (Application Keys)"
echo "  3. the applicationKey      (shown once, at key creation)"
echo

read -r  -p "Bucket name        : " BUCKET
read -rs -p "keyID              : " KEYID;  echo
read -rs -p "applicationKey     : " APPKEY; echo

if [ -z "$BUCKET" ] || [ -z "$KEYID" ] || [ -z "$APPKEY" ]; then
  echo "ABORT: all three values are required."; exit 1
fi

PASSPHRASE=$(openssl rand -base64 32)

umask 077
cat > "$ENV_FILE" <<EOF
# Backblaze B2 credentials for the vault-host restic repository.
# Written by setup-b2.sh. chmod 600. Never commit or copy this file.
RESTIC_REPOSITORY=b2:${BUCKET}:vault
RESTIC_PASSWORD=${PASSPHRASE}
B2_ACCOUNT_ID=${KEYID}
B2_ACCOUNT_KEY=${APPKEY}
EOF
chmod 600 "$ENV_FILE"

echo
echo "=================================================================="
echo " SAVE THIS NOW, IN YOUR PASSWORD MANAGER, BEFORE CONTINUING."
echo " If this passphrase is lost, every backup becomes permanently"
echo " unreadable. There is no recovery path. None."
echo
echo "   restic repository passphrase:"
echo "   $PASSPHRASE"
echo "=================================================================="
echo
read -r -p "Type SAVED once it is in your password manager: " CONFIRM
if [ "$CONFIRM" != "SAVED" ]; then
  echo
  echo "Not confirmed. The file is written and the passphrase is in $ENV_FILE,"
  echo "but a passphrase that exists in only one place is not backed up."
  echo "Save it, then re-run the backup manually."
  exit 1
fi

echo
echo "--- verifying credentials against B2 ---"
set -a; . "$ENV_FILE"; set +a
if restic snapshots >/dev/null 2>&1; then
  echo "OK: repository already exists and credentials work."
elif restic init; then
  echo "OK: repository initialised."
else
  echo "FAILED: could not reach the repository. Check the bucket name and keys."
  echo "Common causes: the key is scoped to a different bucket, or the bucket name is wrong."
  exit 1
fi

echo
echo "--- running the first backup (about 2.6 GB, this will take a few minutes) ---"
"$HOME/automation/bin/backup-vault.sh"
