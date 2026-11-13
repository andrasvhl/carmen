#!/usr/bin/env bash
# Read-only look into the QA sandbox on vault-host, for testers that may not run ssh themselves.
#   sbx.sh cat  <path>          a file under ~/carmen-qa
#   sbx.sh tail <path> [n]      its last n lines (default 20)
#   sbx.sh ls   <path>          a directory listing
#   sbx.sh grep <text> <path>   fixed-string search, at most 50 lines
# <path> is relative to ~/carmen-qa, e.g. .local/state/automation/checkin_app/taps.jsonl or
# vault/Inbox. Nothing here writes, and nothing outside the sandbox can be named.
set -euo pipefail
H=${QA_HOST:-vault-host}
cmd=${1:-}; shift || true
bad() { echo "sbx.sh: $*" >&2; exit 2; }
safe() { case "$1" in ""|/*|*..*|*'~'*) bad "path must be relative to ~/carmen-qa, without .. ($1)";; esac; }
q() { printf '%q' "$1"; }
case "$cmd" in
  cat)  safe "${1:-}"; ssh $H "cat -- ~/carmen-qa/$(q "$1") | head -c 200000" ;;
  tail) safe "${1:-}"; n=${2:-20}; [[ $n =~ ^[0-9]+$ ]] || bad "n must be a number"
        ssh $H "tail -n $n -- ~/carmen-qa/$(q "$1")" ;;
  ls)   safe "${1:-.}"; ssh $H "ls -la -- ~/carmen-qa/$(q "${1:-.}")" ;;
  grep) [ -n "${1:-}" ] || bad "grep needs text"; safe "${2:-}"
        ssh $H "grep -rnF -- $(q "$1") ~/carmen-qa/$(q "$2") | head -50" ;;
  *) bad "usage: sbx.sh cat|tail|ls|grep ..." ;;
esac
