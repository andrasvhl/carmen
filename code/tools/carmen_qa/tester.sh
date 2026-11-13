#!/usr/bin/env bash
# Run one QA tester: a headless Claude that drives one iPhone simulator through a list of stories.
#
#   tester.sh <name> <sim-udid> <stories.md> [WxH points, default 402x874]
#
# It can never stall on an approval: it runs with `--permission-mode dontAsk` and no one to ask,
# so anything outside the allow list below is denied at once and it carries on. It loads no MCP
# server (no connectors), and a watchdog stops it after QA_MAX_MIN minutes in all (default 120)
# or QA_IDLE_MIN minutes without output (default 10). Findings land in
# $QA_RUNS/<date>/<name>/findings.md (default ~/carmen-qa-runs). Doc: vault _system/carmen-qa.md.
set -uo pipefail
[ $# -ge 3 ] || { sed -n 2,11p "$0"; exit 2; }
NAME=$1 SIM=$2 STORIES=$3 SIZE=${4:-402x874}
KIT=$(cd "$(dirname "$0")" && pwd)
OUT=${QA_RUNS:-$HOME/carmen-qa-runs}/$(date +%Y-%m-%d)/$NAME
MAX_MIN=${QA_MAX_MIN:-120} IDLE_MIN=${QA_IDLE_MIN:-10}
mkdir -p "$OUT/shots"
[ -f "$STORIES" ] || { echo "no stories file: $STORIES"; exit 2; }
claude auth status 2>/dev/null | grep -q '"loggedIn": true' || {
  echo "Claude is not signed in on this Mac: run \`claude auth login\` once, then start again."; exit 3; }

# The allow list: the simulator driver, the read-only sandbox helper, image resizing, reading
# anything, writing only inside this tester's folder. Everything else is denied without asking.
python3 - "$KIT" "$OUT" > "$OUT/settings.json" <<'EOF'
import json, sys
kit, out = sys.argv[1], sys.argv[2]
# File rules need "//" before an absolute path; a single "/" means relative to the settings file
# (Nov 9: with one slash every Write was denied and the first tester could not save findings).
allow = [f"Bash({kit}/sim.sh *)", f"Bash({kit}/sbx.sh *)", "Bash(sips *)", f"Bash(cat {kit}/*)",
         "Read", "Glob", "Grep", f"Write(/{out}/**)", f"Edit(/{out}/**)"]
deny = ["WebFetch", "WebSearch", "Agent", "Task", "Workflow", "SendMessage", "RemoteTrigger",
        "CronCreate", "PushNotification", "AskUserQuestion", "mcp__*",
        "Bash(ssh *)", "Bash(scp *)", "Bash(curl *)", "Bash(wget *)"]
print(json.dumps({"permissions": {"allow": allow, "deny": deny}}, indent=1))
EOF

W=${SIZE%x*} H=${SIZE#*x}
python3 - "$KIT/tester-brief.md" "$STORIES" "$SIM" "$SIZE" "$KIT" "$OUT" > "$OUT/brief.md" <<'EOF'
import sys
brief, stories, sim, size, kit, out = sys.argv[1:7]
t = open(brief).read()
for k, v in {"STORIES": open(stories).read(), "SIM": sim, "SIZE": size, "KIT": kit, "OUT": out}.items():
    t = t.replace("{{%s}}" % k, v)
print(t)
EOF

echo "tester $NAME on $SIM: $OUT (max ${MAX_MIN} min, idle ${IDLE_MIN} min)"
cd "$OUT"
SIM=$SIM TAG=$NAME QA_OUT="$OUT/shots" PW=$W PH=$H ENABLE_CLAUDEAI_MCP_SERVERS=false \
  claude -p "$(cat brief.md)" --permission-mode dontAsk --settings "$OUT/settings.json" \
    --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
    --output-format stream-json --verbose ${QA_MODEL:+--model "$QA_MODEL"} \
    > log.jsonl 2> stderr.txt < /dev/null &
PID=$!
start=$(date +%s) why=finished
while kill -0 $PID 2>/dev/null; do
  sleep 20
  now=$(date +%s); last=$(stat -f %m log.jsonl 2>/dev/null || echo $now)
  if (( now - start > MAX_MIN * 60 )); then why="stopped: over ${MAX_MIN} min"; break; fi
  if (( now - last > IDLE_MIN * 60 )); then why="stopped: no output for ${IDLE_MIN} min"; break; fi
done
if kill -0 $PID 2>/dev/null; then
  kill $PID; sleep 5; kill -9 $PID 2>/dev/null
  echo "- P2 · QA harness: tester $why. The last screen and log are in $OUT." >> findings.md
fi
wait $PID 2>/dev/null
python3 - "$why" <<'EOF'
import json, sys, collections
res, denied = None, collections.Counter()
for line in open("log.jsonl", errors="ignore"):
    try: e = json.loads(line)
    except ValueError: continue
    if e.get("type") == "result": res = e
for d in (res or {}).get("permission_denials", []):
    denied[d.get("tool_name")] += 1
import os
# The final report is never lost: if the tester could not write findings.md, its last message is.
if res and res.get("result") and not os.path.exists("findings.md"):
    open("findings.md", "w").write("- P2 · Harness: the tester wrote no findings.md; its final report "
                                   "follows.\n\n" + res["result"] + "\n")
n = sum(1 for l in open("findings.md")) if os.path.exists("findings.md") else 0
if res is None or res.get("is_error"):
    print(f"FAILED: {(res or {}).get('result', 'no result in the log')}"); sys.exit(1)
print(f"{sys.argv[1]}: {n} findings lines, "
      f"{(res or {}).get('num_turns', '?')} turns, denied without asking: {dict(denied) or 'nothing'}")
EOF
