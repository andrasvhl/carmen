#!/usr/bin/env bash
# Run the QA copy of Carmen on 127.0.0.1:8791 inside bwrap. ~/carmen-qa is mounted as the home
# folder, so the real vault, state and credential files are not visible. Only the Claude login
# file is shared (bind), so token refreshes stay in one place.
#
# NOTHING MAY REACH ANOTHER PERSON. Three layers:
#  1. No send credentials in the sandbox (.automation.env stripped, no Gmail/WAHA/Beeper/SSH keys).
#  2. ENABLE_CLAUDEAI_MCP_SERVERS=false: the Claude login would otherwise load the account's
#     claude.ai connectors (Gmail, Calendar, Drive...) server-side. Nov 8: this is how the first
#     run wrote 3 real calendar blocks. Preflight below refuses to start if any MCP server shows.
#  3. /etc/hosts in the sandbox points every local send endpoint at 0.0.0.0.
#
# NOTHING MAY STALL THE RUN. CARMEN_QA_APPROVALS=deny: every Allow card in Carmen's chat is
# denied on its own after 5 s, and a tap on Allow denies too (jobs/checkin_app/chat.py qa_deny).
set -euo pipefail
R=/home/<user>
SB=$R/carmen-qa
{ cat /etc/hosts; for h in gmail.googleapis.com smtp.gmail.com api.pushover.net api.telegram.org \
    graph.facebook.com api.beeper.com matrix.beeper.com slack.com api.twilio.com api.sendgrid.com; do
    echo "0.0.0.0 $h"; done; } > $SB/qa-hosts

box() {
  bwrap \
    --ro-bind /usr /usr --symlink usr/lib /lib --symlink usr/lib64 /lib64 \
    --symlink usr/bin /bin --symlink usr/sbin /sbin \
    --ro-bind /etc /etc --ro-bind $SB/qa-hosts /etc/hosts --ro-bind /run/systemd/resolve /run/systemd/resolve \
    --ro-bind /sys /sys --proc /proc --dev /dev --tmpfs /tmp --tmpfs /var/tmp \
    --bind $SB $R \
    --ro-bind $R/.local/share/claude $R/.local/share/claude \
    --bind $R/.claude/.credentials.json $R/.claude/.credentials.json \
    --unshare-pid --unshare-ipc --die-with-parent --new-session \
    --clearenv --setenv HOME $R --setenv USER owner --setenv PATH $R/.local/bin:/usr/local/bin:/usr/bin:/bin \
    --setenv LANG C.UTF-8 --setenv PYTHONUNBUFFERED 1 --setenv CHECKIN_PORT 8791 \
    --setenv ENABLE_CLAUDEAI_MCP_SERVERS false \
    --setenv CARMEN_QA_APPROVALS deny \
    --chdir "$1" "${@:2}"
}

# Preflight: the agent must see no MCP server at all, or nothing starts.
out=$(box $R/vault timeout 90 claude mcp list 2>&1 || true)
if ! grep -q "No MCP servers configured" <<<"$out"; then
  echo "PREFLIGHT FAILED: the sandbox agent can see MCP servers:"; echo "$out"; exit 1
fi
echo "preflight ok: no MCP servers visible"
exec_box() { box $R/automation $R/automation/bin/run-job checkin_app; }
exec_box
