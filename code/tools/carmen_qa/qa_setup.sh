#!/usr/bin/env bash
# Build a sealed QA copy of Carmen at ~/carmen-qa (run on vault-host).
set -euo pipefail
R=/home/<user>
SB=$R/carmen-qa
[ -e "$SB" ] && { echo "exists: $SB"; exit 1; }
mkdir -p $SB/{.config/plan-today,.google_workspace_mcp,.claude/projects,.local/state,.local/bin,.local/share/claude}

cp ~/deny.json $SB/deny.json
rsync -a --exclude .git $R/vault/ $SB/vault/
rsync -a $R/automation/ $SB/automation/
rsync -a $R/.local/state/automation/ $SB/.local/state/automation/
# Flags under QA (Nov 11): on in every sandbox while still off live, so a QA run tests them first.
# Once it is on live (state/checkin_app/flags.json), take it off this list.
QA_FLAGS_ON="step_order"
python3 - "$SB/.local/state/automation/checkin_app/flags.json" $QA_FLAGS_ON <<'PYEOF'
import json, sys, pathlib
p = pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True, exist_ok=True)
try:
    d = json.loads(p.read_text())
except (OSError, ValueError):
    d = {}
d.update({n: True for n in sys.argv[2:]})
p.write_text(json.dumps(d))
PYEOF
cp $R/.config/plan-today/google-calendar.json $SB/.config/plan-today/
cp $R/.google_workspace_mcp/client_secret.json $SB/.google_workspace_mcp/
# Only the client secret: no Gmail credentials, so mail sends fail.

cp $R/.claude/CLAUDE.md $SB/.claude/
cp -a $R/.claude/skills $SB/.claude/
rsync -a $R/.claude/projects/-home-<user>-vault $SB/.claude/projects/
python3 - <<'EOF'
import json
R="/home/<user>"; SB=R+"/carmen-qa"
s=json.load(open(R+"/.claude/settings.json")); s.get("hooks",{}).pop("SessionStart",None)
s["permissions"]=json.load(open(SB+"/deny.json"))  # layer (d): hard deny rules
json.dump(s,open(SB+"/.claude/settings.json","w"),indent=2)
d=json.load(open(R+"/.claude.json")); d["mcpServers"]={}
for p in d.get("projects",{}).values(): p["mcpServers"]={}
json.dump(d,open(SB+"/.claude.json","w"))
import secrets
src={}
for l in open(R+"/.automation.env"):
    l=l.strip()
    if l and not l.startswith("#") and "=" in l:
        k,v=l.split("=",1); src[k]=v
keep=["GROQ_API_KEY","OPENAI_API_KEY","TELEGRAM_OWNER_ID"]
out=[f"{k}={src[k]}" for k in keep if k in src]
pw=secrets.token_urlsafe(12)
out+= [f"CHECKIN_PASSWORD={pw}", f"CHECKIN_SECRET={secrets.token_hex(32)}",
       f"CARMEN_TALK_TOKEN={secrets.token_hex(16)}"]
for k in src:
    if k.startswith(("CARMEN_TTS","ELEVENLABS","TG_MODEL")): out.append(f"{k}={src[k]}")
open(SB+"/.automation.env","w").write("\n".join(out)+"\n")
open(SB+"/QA-LOGIN.txt","w").write(pw+"\n")
EOF
chmod 600 $SB/.automation.env $SB/QA-LOGIN.txt
ln -s $R/.local/share/claude/versions/$(readlink $R/.local/bin/claude | xargs basename) $SB/.local/bin/claude
touch $SB/.claude/.credentials.json
du -sh $SB
