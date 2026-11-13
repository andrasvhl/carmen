#!/usr/bin/env python3
"""What pushes, what only mails, and what a failed push leaves behind.

    ~/automation/.venv/bin/python -m tests.urgent_push

Written because mail alone buried every alert that mattered: the Claude Code
expiry warning, the WhatsApp ban tripwire and two crashed `digest` runs all
sat unread in an inbox the owner does not open promptly. He reads push.

  1. send_urgent pushes, then mails the same alert as the record (still used
     by wa_consumer and claude-auth-watch).
  2. A failed push is never silent: the mail subject says [PUSH FAILED].
  3. push_or_mail (2026-10-03, the maintenance brief): a delivered push sends
     no mail at all; a failed one mails with [PUSH FAILED].
  4. Only a CRITICAL check pushes from a job report, and it no longer mails.
     Important and routine go to the alert log (tests/alert_log_routing).
"""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from runner import config

TMP = pathlib.Path(tempfile.mkdtemp())
config.STATE_DIR = TMP
config.VAULT = TMP / "vault"
config.INBOX = config.VAULT / "Inbox"
config.INBOX.mkdir(parents=True)
(config.VAULT / "_system").mkdir()

from runner import notify
from runner.job import Job, CRITICAL, IMPORTANT, YOU

fails = []
pushes, mails = [], []
push_ok = True


def fake_push(title, message, **kw):
    if not push_ok:
        raise notify.PushUnavailable("stub refused")
    pushes.append((title, message, kw.get("priority", 0)))


def fake_mail(subject, body):
    mails.append((subject, body))
    return "to@example.com"


notify.send_push = fake_push
notify.send_mail = fake_mail


def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if not ok and detail else ""))
    if not ok:
        fails.append(label)


def reset():
    pushes.clear()
    mails.clear()


# --- 1. push, then mail as the record ---------------------------------------
reset()
notify.send_urgent("vault-host: thing broke", "long body", push_message="short", priority=1)
check("send_urgent pushes once", len(pushes) == 1, str(pushes))
check("the push title drops the 'vault-host: ' prefix", pushes and pushes[0][0] == "thing broke",
      str(pushes))
check("the push carries the short message, not the mail body",
      pushes and pushes[0][1] == "short", str(pushes))
check("the priority reaches Pushover", pushes and pushes[0][2] == 1, str(pushes))
check("the mail still goes, subject unchanged",
      mails == [("vault-host: thing broke", "long body")], str(mails))

# --- 2. a failed push says so in the mail -----------------------------------
reset()
push_ok = False
notify.send_urgent("vault-host: thing broke", "long body", push_message="short")
push_ok = True
check("a failed push still mails", len(mails) == 1, str(mails))
check("the subject says the push failed",
      mails and mails[0][0] == "[PUSH FAILED] vault-host: thing broke", str(mails))
check("the body says why", mails and "stub refused" in mails[0][1], str(mails))


# --- 3. push_or_mail: push only, mail only on failure ----------------------
reset()
to = notify.push_or_mail("vault-host: thing broke", "long body", push_message="short", priority=1)
check("push_or_mail pushes once", len(pushes) == 1 and pushes[0][2] == 1, str(pushes))
check("a delivered push sends no mail", mails == [], str(mails))
check("push_or_mail says it pushed", to == "push", str(to))

reset()
push_ok = False
to = notify.push_or_mail("vault-host: thing broke", "long body", push_message="short")
push_ok = True
check("a failed push falls back to one mail", len(mails) == 1, str(mails))
check("...whose subject says the push failed",
      mails and mails[0][0] == "[PUSH FAILED] vault-host: thing broke", str(mails))
check("...and push_or_mail returns the recipient", to == "to@example.com", str(to))


# --- 4. only CRITICAL pushes from a job report ------------------------------
def report(tier):
    reset()
    j = Job(f"urgent_{tier}", description="Test job", dry_run=False)
    j.check("a thing is broken", ok=False, detail="d", tier=tier, owner=YOU)
    j._report()


report(CRITICAL)
check("a CRITICAL failure pushes", len(pushes) == 1, str(pushes))
check("the CRITICAL push is high priority", pushes and pushes[0][2] == 1, str(pushes))
check("the CRITICAL push names the check", pushes and "a thing is broken" in pushes[0][1],
      str(pushes))
check("a CRITICAL failure no longer mails when the push lands", mails == [], str(mails))

report(IMPORTANT)
check("an IMPORTANT failure does not push", pushes == [], str(pushes))
check("an IMPORTANT failure does not mail", mails == [], str(mails))

# --- anything a test really delivers says [TEST] (2026-10-04) ---------------------
# The real functions, not the stubs above: this is the net under a missed stub.
import importlib
import io
import json as _json
from urllib.parse import parse_qs

real = importlib.reload(notify)
check("a test run is recognised as one", real.in_test())
check("the label is added once", real._label("x") == "[TEST] x"
      and real._label("[TEST] x") == "[TEST] x")
sent = []


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capture(req, timeout=0):
    sent.append(parse_qs(req.data.decode()))
    return _Resp(_json.dumps({"status": 1, "ok": True, "result": {"message_id": 1}}).encode())


real.config.load_env = lambda: {"PUSHOVER_USER": "u", "PUSHOVER_TOKEN": "t",
                                "TELEGRAM_BOT_TOKEN": "1:x", "TELEGRAM_OWNER_ID": "1"}
real.urllib.request.urlopen = _capture
real.send_push("WhatsApp BAN TRIPWIRE", "body")
check("a real push from a test is titled [TEST]",
      sent and sent[-1]["title"] == ["[TEST] WhatsApp BAN TRIPWIRE"], sent)
real.send_telegram("Maintenance: 2 need you", silent=False)
check("a real Telegram message from a test starts with [TEST]",
      sent and sent[-1]["text"][0].startswith("[TEST] Maintenance"), sent)

print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'all passed'}")
sys.exit(1 if fails else 0)
