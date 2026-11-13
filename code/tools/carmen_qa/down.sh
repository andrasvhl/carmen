#!/usr/bin/env bash
# Tear down a Carmen QA sandbox: list what its agent called outside the copy, stop it, delete it.
# Counts only transcripts written during the run (newer than QA-LOGIN.txt, made at setup),
# sub-agents included; the copied history from the real ~/.claude would otherwise drown it.
set -uo pipefail
H=vault-host
echo "Connector and MCP tool calls made by the sandbox during this run (should be none):"
ssh $H 'cd ~/carmen-qa 2>/dev/null && find .claude/projects -name "*.jsonl" -newer QA-LOGIN.txt -print0 |
  xargs -0 -r grep -ho "\"name\": *\"mcp__[^\"]*\"" | sort | uniq -c | sort -rn' || true
echo "Allow cards denied automatically during the run (what Claude tried to do):"
ssh $H 'cd ~/carmen-qa 2>/dev/null && python3 - <<EOF
import json, os
since = os.path.getmtime("QA-LOGIN.txt")
import datetime as dt
n = 0
for line in open(".local/state/automation/checkin_app/taps.jsonl", errors="ignore"):
    if "chat_qa_denied" not in line: continue
    e = json.loads(line)
    if dt.datetime.fromisoformat(e["at"]).timestamp() < since: continue
    n += 1; print("  " + e["at"][11:16] + "  " + str(e.get("tool")) + ": " + str(e.get("shown"))[:120])
print("  " + str(n) + " in all")
EOF' || true
ssh $H 'for p in $(pgrep -x bwrap); do grep -q carmen-qa /proc/$p/cmdline && kill $p; done; sleep 2; rm -rf ~/carmen-qa'
pkill -f "8791:127.0.0.1:8791" 2>/dev/null || true
echo "Sandbox removed. Shut down extra simulators with: xcrun simctl shutdown <udid>"
