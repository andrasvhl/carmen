#!/usr/bin/env bash
# Start a Carmen QA sandbox from the Mac in one go. Doc: vault _system/carmen-qa.md.
# Builds ~/carmen-qa on vault-host (a copy of vault + state + code), starts it on 127.0.0.1:8791
# inside bwrap with the four isolation layers, checks isolation, and tunnels it to localhost:8791.
# Prints the sandbox login at the end. Nothing in the sandbox can reach another person.
set -euo pipefail
cd "$(dirname "$0")"
H=vault-host

ssh $H 'test ! -e ~/carmen-qa' || { echo "~/carmen-qa already exists on $H: run down.sh first"; exit 1; }
free=$(ssh $H "df --output=avail -BG ~ | tail -1 | tr -dc 0-9")
[ "$free" -ge 6 ] || { echo "only ${free} GB free on $H, need 6"; exit 1; }

scp -q qa_setup.sh qa_run.sh deny.json $H:
ssh $H '~/qa_setup.sh && mv ~/qa_setup.sh ~/qa_run.sh ~/carmen-qa/ && rm ~/deny.json'
ssh $H 'cd ~/carmen-qa && (setsid nohup ./qa_run.sh > qa.log 2>&1 < /dev/null &)'
for i in $(seq 1 30); do
  ssh $H 'curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8791/checkin/login' | grep -q 200 && break
  ssh $H 'grep -q "PREFLIGHT FAILED" ~/carmen-qa/qa.log' && { ssh $H 'cat ~/carmen-qa/qa.log'; exit 1; }
  sleep 4
done
ssh $H 'grep -q "preflight ok" ~/carmen-qa/qa.log' || { echo "preflight did not pass"; exit 1; }

# Isolation check from inside the sandbox's mount namespace.
ssh $H 'C=$(for p in $(pgrep -x bwrap); do grep -q carmen-qa /proc/$p/cmdline && pgrep -P $p | head -1; done | head -1)
sudo nsenter -t $C -m -S 1000 -G 1000 -- env HOME=/home/<user> sh -c "cd /home/<user>/automation && .venv/bin/python -" <<EOF
import os, sys, urllib.request
sys.path.insert(0, ".")
bad = []
if os.path.exists("/home/<user>/vault/.git"): bad.append("real vault visible")
from runner import notify
try: notify.send_push("QA isolation test", "must not arrive"); bad.append("push worked")
except Exception: pass
if os.path.exists("/home/<user>/.google_workspace_mcp/credentials"): bad.append("gmail credentials present")
for u in ["http://127.0.0.1:3000/api/sessions", "http://127.0.0.1:8787/mcp"]:
    try: urllib.request.urlopen(u, timeout=5); bad.append(u + " open")
    except Exception: pass
import socket
for h in ["gmail.googleapis.com", "api.pushover.net", "api.telegram.org"]:
    if socket.gethostbyname(h) != "0.0.0.0": bad.append(h + " resolves")
print("ISOLATION OK" if not bad else "ISOLATION FAILED: " + "; ".join(bad))
EOF' | tee /tmp/carmen-qa-iso.txt
grep -q "ISOLATION OK" /tmp/carmen-qa-iso.txt || { echo "stopping: isolation failed"; ./down.sh; exit 1; }

pkill -f "8791:127.0.0.1:8791" 2>/dev/null || true
ssh -f -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 8791:127.0.0.1:8791 $H
echo "Sandbox up: http://localhost:8791/checkin/   login: $(ssh $H cat carmen-qa/QA-LOGIN.txt)"
echo "Browser: localStorage['checkin-live']='1'. Simulator: DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer, see sim.sh."
