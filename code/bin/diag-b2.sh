#!/usr/bin/env bash
# Diagnose a B2 401 WITHOUT revealing any secret value.
# Reports only lengths, character classes and whitespace problems.
set -uo pipefail
ENV_FILE="$HOME/.config/restic/b2-box.env"
[ -r "$ENV_FILE" ] || { echo "no env file at $ENV_FILE"; exit 1; }
set -a; . "$ENV_FILE"; set +a

describe() {
  local name="$1" val="$2"
  local len=${#val}
  local trimmed; trimmed="$(printf '%s' "$val" | tr -d '[:space:]')"
  local tlen=${#trimmed}
  local cls="mixed"
  case "$trimmed" in
    *[!0-9a-fA-F]*) cls="not-pure-hex" ;;
    *) cls="hex-only" ;;
  esac
  printf "  %-18s length=%-3s  after-stripping-whitespace=%-3s  charset=%-12s" "$name" "$len" "$tlen" "$cls"
  if [ "$len" -ne "$tlen" ]; then printf "  <-- CONTAINS WHITESPACE"; fi
  printf "\n"
}

echo "=== credential shape (no values shown) ==="
describe "B2_ACCOUNT_ID"  "${B2_ACCOUNT_ID:-}"
describe "B2_ACCOUNT_KEY" "${B2_ACCOUNT_KEY:-}"
echo "  RESTIC_REPOSITORY = ${RESTIC_REPOSITORY:-<unset>}"
echo
echo "=== expected shapes for Backblaze B2 ==="
echo "  application keyID     : 25 chars   (this is what restic needs)"
echo "  master/account ID     : 12 chars   (WRONG value to use with an application key)"
echo "  applicationKey secret : 31 chars   (usually begins K00...)"
echo
echo "=== live auth test against B2 (values never printed) ==="
code=$(curl -s -o /tmp/b2auth.json -w "%{http_code}" \
  -u "${B2_ACCOUNT_ID}:${B2_ACCOUNT_KEY}" \
  https://api.backblazeb2.com/b2api/v3/b2_authorize_account)
echo "  HTTP $code"
if [ "$code" = "200" ]; then
  echo "  AUTH OK. The keys are valid, so the 401 was from stale values at the time restic ran."
  python3 -c "
import json
d=json.load(open('/tmp/b2auth.json'))
caps=(d.get('apiInfo',{}).get('storageApi',{}) or {})
print('  key scope     :', 'ALL buckets' if not caps.get('bucketId') else 'single bucket: '+str(caps.get('bucketName')))
print('  capabilities  :', ','.join(caps.get('capabilities',[])[:12]))
" 2>/dev/null
else
  echo "  AUTH FAILED. Backblaze says:"
  python3 -c "
import json
try:
    d=json.load(open('/tmp/b2auth.json'))
    print('   ', d.get('code'), '-', d.get('message'))
except Exception:
    print('    (no JSON body)')
" 2>/dev/null
fi
rm -f /tmp/b2auth.json
