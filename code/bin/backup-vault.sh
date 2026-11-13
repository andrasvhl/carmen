#!/usr/bin/env bash
# Nightly restic backup of the Obsidian vault from vault-host to Backblaze B2.
#
# WHY: until 2026-09-02 Obsidian Sync was the vault's ONLY copy. A client bug deleted
# 1,886 files across two incidents and Sync version history was the sole reason they
# came back. This is the hedge. It also covers files over the 199MB Sync cap, which
# Sync structurally cannot hold.
#
# Credentials live in ~/.config/restic/b2-box.env (chmod 600), never in this script.

set -uo pipefail
ENV_FILE="$HOME/.config/restic/b2-box.env"
VAULT="$HOME/vault"
LOG_TAG="[backup-vault]"

log() { echo "$LOG_TAG $*"; }

if [ ! -r "$ENV_FILE" ]; then
  log "FATAL: $ENV_FILE missing or unreadable. Backups are NOT running."
  exit 1
fi
# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

for v in RESTIC_REPOSITORY RESTIC_PASSWORD B2_ACCOUNT_ID B2_ACCOUNT_KEY; do
  val="${!v:-}"
  if [ -z "$val" ]; then
    log "FATAL: $v is unset in $ENV_FILE."; exit 1
  fi
  case "$val" in
    PASTE_*) log "FATAL: $v is still a placeholder in $ENV_FILE."; exit 1 ;;
  esac
done

if ! restic snapshots >/dev/null 2>&1; then
  log "repository not initialised, running restic init"
  restic init || { log "FATAL: restic init failed"; exit 1; }
fi

log "backing up $VAULT"
restic backup "$VAULT" \
  --tag vault --tag vault-host \
  --exclude '.obsidian/workspace*' \
  --exclude '**/.git' \
  --exclude '**/__pycache__' \
  --exclude '**/node_modules' \
  --exclude '**/.venv'
rc=$?

if [ $rc -ne 0 ]; then
  log "BACKUP FAILED (exit $rc)"
  ts=$(date +%Y-%m-%dT%H:%M); day=$(date +%Y-%m-%d)
  mkdir -p "$VAULT/Inbox"
  {
    echo "---"; echo "type: reference"; echo "captured: $ts"; echo "---"; echo
    echo "# Vault backup FAILED on vault-host ($day)"; echo
    echo "restic exited $rc. The vault is currently protected only by Obsidian Sync."
    echo "Check: journalctl -u backup-vault -n 50"; echo
    echo "Background: [[_system/vps-vault-host]]."
  } > "$VAULT/Inbox/Vault backup failed ($day).md" 2>/dev/null || true
  exit $rc
fi

log "pruning old snapshots"
restic forget --tag vault --prune \
  --keep-daily 7 --keep-weekly 5 --keep-monthly 12 --keep-yearly 3 || log "warning: prune failed"

log "done. snapshots now:"
restic snapshots --compact 2>/dev/null | tail -5
