#!/usr/bin/env bash
# Preflight guard for the Obsidian headless sync client (ob).
#
# WHY THIS EXISTS
#   ob <=0.0.14 in bidirectional mode cannot distinguish "I never had this file"
#   from "the user deleted this file". If its local copy is short, a restart makes it
#   PUSH DELETIONS for everything it lacks instead of downloading them.
#   2026-08-28: 316 attachments destroyed. 2026-09-01: 1570 more files.
#   Root cause both times: the local copy was legitimately incomplete (a folder
#   exclusion had been removed) and a service restart reconciled the gap downward.
#
# WHAT IT DOES
#   Before every start, compare the local file count against _system/sync-manifest.json.
#   If local is materially short, force pull-only: the client can then download but can
#   never delete. Once caught up, the next start restores bidirectional automatically.
#
#   It NEVER exits non-zero. systemd has Restart=always, so failing here would loop.
#   The safe outcome is "run read-only", not "refuse to run".
#
#   GUARD_DRY_RUN=1 prints the decision and exits without touching config or starting sync.

set -uo pipefail
VAULT="$HOME/vault"
MANIFEST="$VAULT/_system/sync-manifest.json"
THRESHOLD=97      # percent of expected files required before writes are allowed
MAX_AGE_DAYS=21   # a stale manifest is as dangerous as none: the vault grows, it does not

log() { echo "[sync-guard] $*"; }

local_count=$(find "$VAULT" -type f -not -path "*/.*" 2>/dev/null | wc -l)
expected=$(MANIFEST="$MANIFEST" MAX_AGE_DAYS="$MAX_AGE_DAYS" python3 - <<'PY' 2>/dev/null
import json, os, datetime
try:
    m = json.load(open(os.environ["MANIFEST"]))
    gen = datetime.datetime.fromisoformat(m["generated"])
    now = datetime.datetime.now(gen.tzinfo)
    print(0 if (now - gen).days > int(os.environ["MAX_AGE_DAYS"]) else m["syncable_count"])
except Exception:
    print(0)
PY
)

mode=bidirectional
reason=""
if [ "${expected:-0}" -le 0 ]; then
  mode=pull-only
  reason="manifest missing, unreadable, or older than ${MAX_AGE_DAYS}d at $MANIFEST"
else
  floor=$(( expected * THRESHOLD / 100 ))
  if [ "$local_count" -lt "$floor" ]; then
    mode=pull-only
    reason="local copy is short: $local_count files vs $expected expected (floor $floor)"
  fi
fi

if [ "$mode" = pull-only ]; then
  log "REFUSING WRITES. $reason"
  log "Starting pull-only: can download, cannot delete. Returns to bidirectional once caught up."
else
  log "OK: $local_count local files vs $expected expected. Writes allowed."
fi

if [ "${GUARD_DRY_RUN:-0}" = "1" ]; then
  log "DRY RUN: would set mode=$mode and start ob sync --continuous"
  exit 0
fi

if [ "$mode" = pull-only ]; then
  ts=$(date +%Y-%m-%dT%H:%M); day=$(date +%Y-%m-%d)
  mkdir -p "$VAULT/Inbox"
  {
    echo "---"; echo "type: reference"; echo "captured: $ts"; echo "---"; echo
    echo "# Sync guard blocked bidirectional on vault-host"; echo
    echo "**$reason**"; echo
    echo "Local files: $local_count - expected: $expected"; echo
    echo "The box started in pull-only so it could not delete anything remotely."
    echo "It returns to bidirectional on its own once it has caught up."
    echo "If the cause is a stale manifest, regenerate it from the Mac."; echo
    echo "Background: [[_system/vps-vault-host]]."
  } > "$VAULT/Inbox/Sync guard blocked bidirectional ($day).md" 2>/dev/null || true
fi

# Keep the manifest fresh, but only ever RATCHET UP.
# Raising it when the box already holds more files than recorded is always safe: it means
# the vault grew and the manifest lagged. Lowering it is never safe, because that is exactly
# how a real gap would be normalised into the baseline. So this only ever increases.
if [ "$mode" = bidirectional ] && [ "$local_count" -gt "${expected:-0}" ]; then
  if MANIFEST="$MANIFEST" NEWCOUNT="$local_count" python3 - <<'PYREFRESH' 2>/dev/null
import json, os, datetime
p = os.environ["MANIFEST"]; new = int(os.environ["NEWCOUNT"])
m = json.load(open(p))
if new > m.get("syncable_count", 0):
    m["syncable_count"] = new
    m["generated"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    m["generated_by"] = "vault-host (ratchet)"
    json.dump(m, open(p, "w"), indent=1, ensure_ascii=False)
PYREFRESH
  then log "manifest ratcheted up to $local_count"; fi
fi

/usr/bin/ob sync-config --path "$VAULT" --mode "$mode" >/dev/null 2>&1 || log "warning: could not set mode=$mode"
log "starting: ob sync --continuous (mode=$mode)"
exec /usr/bin/ob sync --path "$VAULT" --continuous
