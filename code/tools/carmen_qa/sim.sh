#!/usr/bin/env bash
# sim.sh tap X Y | type "text" | swipe X1 Y1 X2 Y2 | key N | shot NAME   (points on a 402x874 iPhone 18 Pro)
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
S=${SIM:?set SIM}
Q=${QA_OUT:-/tmp/carmen-qa-shots}; mkdir -p "$Q"
IDB=~/.idb-venv/bin/idb
# idb talks to a per-simulator companion process; start it if it is not running.
if [ "$1" != shot ] && [ ! -S "/tmp/idb/${S}_companion.sock" ]; then
  mkdir -p /tmp/idb
  nohup "$(command -v idb_companion || echo /opt/homebrew/bin/idb_companion)" --udid "$S" --grpc-domain-sock "/tmp/idb/${S}_companion.sock" \
    > "/tmp/idb/${S}_companion.log" 2>&1 &
  for _ in 1 2 3 4 5 6 7 8 9 10; do [ -S "/tmp/idb/${S}_companion.sock" ] && break; sleep 1; done
fi
case "$1" in
  tap)   $IDB ui tap --udid $S "$2" "$3" ;;
  type)  $IDB ui text --udid $S "$2" ;;
  swipe) $IDB ui swipe --udid $S "$2" "$3" "$4" "$5" --duration "${6:-0.3}" ;;
  key)   $IDB ui key --udid $S "$2" ;;
  shot)  xcrun simctl io $S screenshot "$Q/${TAG:-x}-$2.png" >/dev/null 2>&1
         sips -z ${PH:?} ${PW:?} "$Q/${TAG:-x}-$2.png" --out "$Q/${TAG:-x}-$2.png" >/dev/null; echo "$Q/${TAG:-x}-$2.png" ;;
esac
