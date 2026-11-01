#!/usr/bin/env python3
"""Send in the check-in app's Replies (Oct 31), offline: plan-today's mailer.py over a fake Gmail,
then the server's mailprep and sendmail through Starlette's test client. Nothing reaches Gmail:
the fake answers every call and records what would have been sent. Exit 1 if any check failed.

    .venv/bin/python -m tests.checkin_app_send
"""
import base64
import datetime as dt
import email
import json
import pathlib
import sys
import tempfile
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import jobs.checkin_app as app
from tests.tg_bridge._fakes import Checks

check = Checks("check-in app: send mail")
tmp = pathlib.Path(tempfile.mkdtemp())
app.LOG = tmp / "taps.jsonl"
app.JOURNAL_DIR = tmp / "journal"
app.config.INBOX = tmp / "Inbox"
app.config.INBOX.mkdir()

mailer = app._mailer()
ME, SARAH, GMAIL = "<YOUR_EMAIL>", "sarah@example.com", "<YOUR_PERSONAL_EMAIL>"
THREAD = {"id": "t1", "messages": [
    {"id": "m1", "internalDate": "1761900000000", "snippet": "Coffee next week?", "payload": {"headers": [
        {"name": "From", "value": f"Quinn Chen <{SARAH}>"}, {"name": "To", "value": ME},
        {"name": "Cc", "value": "bob@example.com"}, {"name": "Subject", "value": "Coffee"},
        {"name": "Message-ID", "value": "<m1@mail.example.com>"},
        {"name": "References", "value": "<m0@mail.example.com>"}]}}]}
gmail = {"sent": [], "late": False}


def fake_api(acct, path, body=None):
    if path == "messages/send":
        gmail["sent"].append((acct, body))
        return {"id": "g1", "threadId": body.get("threadId", "new")}
    if path.startswith("threads?"):
        q = urllib.parse.parse_qs(path.split("?", 1)[1])["q"][0]
        if "subject:" in q:   # exact subject
            return {"threads": [{"id": "t1"}]} if acct == ME and 'subject:"Coffee"' in q else {}
        if SARAH not in q:
            return {}
        if "365d" in q:       # related(): a year of her mail, an old trip in Gmail too
            return {"threads": [{"id": "t1"}] if acct == ME else [{"id": "t9"}]}
        return {"threads": [{"id": "t1"}]} if acct == ME else {}
    if path.startswith("messages?"):   # pick_mailbox's earlier mail: the Gmail one is newer
        return {"messages": [{"id": "m1" if acct == ME else "m9"}]} if SARAH in urllib.parse.unquote_plus(path) else {}
    if path.startswith("messages/m"):
        return {"internalDate": "1761900000000" if "m1" in path else "1771900000000"}
    if path.startswith("threads/t9"):
        return {"id": "t9", "messages": [{"id": "m9", "internalDate": "1771900000000", "payload": {"headers": [
            {"name": "Subject", "value": "Old trip"}, {"name": "From", "value": f"Quinn Chen <{SARAH}>"}]}}]}
    if path.startswith("threads/t1"):
        th = json.loads(json.dumps(THREAD))
        if gmail["late"]:   # Quinn wrote again after the draft
            late = json.loads(json.dumps(th["messages"][0]))
            late.update(id="m2", internalDate=str(int(2e12)), snippet="Actually, Friday?")
            th["messages"].append(late)
        return th
    raise AssertionError("unexpected Gmail call " + path)


mailer._api = fake_api
mailer._token = lambda acct: "tok"


def decoded(body):
    return email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]))


# 1. The MIME and the threading.
res = mailer.prepare({"acct": ME, "to": "Quinn Chen", "subj": "Coffee", "body": "Tuesday 3pm works."}, since=0, force=True)
check("a name finds the thread and her address", res["ok"] and res["to"] == [SARAH] and res["thread"], res)
check("everyone else on the last mail goes in cc, never himself", res["cc"] == ["bob@example.com"], res)
check("the thread id comes back for the sheet", res["thread_id"] == "t1", res)
check("the thread so far comes back too", [(h["from"], h["mine"], h["snippet"]) for h in res["history"]] == [("Quinn Chen", False, "Coffee next week?")], res["history"])
mailer.commit(res["id"])
acct, body = gmail["sent"].pop()
m = decoded(body)
check("sent in the thread", body.get("threadId") == "t1")
check("In-Reply-To is the last message", m["In-Reply-To"] == "<m1@mail.example.com>", m["In-Reply-To"])
check("References chain the thread", m["References"] == "<m0@mail.example.com> <m1@mail.example.com>", m["References"])
check("Re: subject, From his mailbox", m["Subject"] == "Re: Coffee" and m["From"] == ME, (m["Subject"], m["From"]))
check("the body is his text", m.get_payload(decode=True).decode().strip() == "Tuesday 3pm works.")

res = mailer.prepare({"acct": ME, "to": "Quinn Chen", "subj": "x", "body": "b", "thread_id": "t1"}, force=True)
check("a known thread id is used even when the subject changed", res["ok"] and res["thread_id"] == "t1", res)
res = mailer.prepare({"acct": ME, "to": SARAH, "subj": "Coffee", "body": "b", "new_thread": True}, force=True)
mailer.commit(res["id"])
acct, body = gmail["sent"].pop()
m = decoded(body)
check("new mail: no threadId, no In-Reply-To, subject as written",
      "threadId" not in body and m["In-Reply-To"] is None and m["Subject"] == "Coffee", (body.keys(), m["Subject"]))
res = mailer.prepare({"acct": ME, "to": SARAH, "subj": "Coffee!", "body": "b"}, force=True)
check("a reworded subject still finds her thread", res["ok"] and res["thread_id"] == "t1", res)
res = mailer.prepare({"acct": ME, "to": SARAH, "subj": "Invoice for October", "body": "b"}, force=True)
check("an unrelated subject to her is a new mail", res["ok"] and not res["thread"], res)
mailer._pending.clear()
check("mailbox_for finds the mailbox holding the thread", mailer.mailbox_for("Coffee", SARAH) == ME)
check("mailbox_for is empty with no mail with them anywhere", mailer.mailbox_for("Nothing", "nobody@example.com") == "")
check("the thread's mailbox wins over the draft's", mailer.pick_mailbox("Coffee", SARAH, prefer=GMAIL) == (ME, "thread"))
check("no thread: the mailbox with the latest mail with them", mailer.pick_mailbox("Invoice", SARAH, prefer=ME) == (GMAIL, "earlier mail"))
check("nothing with them: the draft's mailbox", mailer.pick_mailbox("x", "nobody@example.com", prefer=ME) == (ME, "draft"))
rel = mailer.related(SARAH, "t1")
check("related lists other mail with them, not this thread", [(r["acct"], r["subj"]) for r in rel] == [(GMAIL, "Old trip")], rel)
check("a reply since the draft holds the send", (gmail.update(late=True) or True)
      and mailer.prepare({"acct": ME, "to": SARAH, "subj": "Coffee", "body": "b"}, since=1.8e9).get("held"))
gmail["late"] = False
mailer._pending.clear()

# 2. The server path.
from starlette.testclient import TestClient

app.config.load_env = lambda: {"CHECKIN_PASSWORD": "pw", "CHECKIN_SECRET": "s3cret"}
hidden = []
app.save_override = lambda kind, day, value: hidden.append((kind, value))
app._dropped_mod = lambda: type("D", (), {"load": staticmethod(lambda: []), "check": staticmethod(lambda r, g: None),
                                         "record": staticmethod(lambda *a, **k: None)})
NOTE = app.config.INBOX / "Reply to Quinn.md"
NOTE_TEXT = "---\ntype: action\nstatus: open\npriority: 3-normal\n---\n## Next action\n- [ ] Next: reply to Quinn (2 min)\n"
NOTE.write_text(NOTE_TEXT)
DRAFT = {"via": "mail", "acct": ME, "to": "Quinn Chen", "subj": "Coffee", "body": "Tuesday 3pm works."}
rows = [{"lk": "Reply to Quinn", "n": "Reply to Quinn", "p": "N", "g": "today", "ne": 2,
         "ch": [{"t": "Send the reply", "msg": DRAFT}]},
        {"n": "Answer Tom", "p": "N", "g": "today", "ne": 2,
         "ch": [{"t": "Send", "msg": {"via": "mail", "to": "tom@example.com", "subj": "Dinner", "body": "How about [slot]?",
                                      "needs_slot": True}}]},
        {"n": "Text Lu", "p": "N", "g": "today", "ne": 2,
         "ch": [{"t": "Send", "msg": {"via": "whatsapp", "to": "Lu", "body": "hey!"}}]}]
app.load_prerun = lambda path=None, overlay=True: {"rows": [["Today", rows]], "generated": "2026-10-31T08:00-07:00"}

client = TestClient(app.make_app())
client.cookies.set(app.COOKIE, app.make_cookie("s3cret"))
BASE = "/checkin" if any(getattr(r, "path", "").startswith("/checkin") for r in client.app.routes) else ""


def act(**body):
    r = client.post(f"{BASE}/api/act", json={"day": "today", "tab": "replies", **body})
    return r.status_code, r.json()


view = client.get(f"{BASE}/api/view").json()
card = next((r for r in view["replies"] if r["id"] == "Reply to Quinn"), None)
check("the reply card carries acct and the draft's time", card and card["draft"]["acct"] == ME
      and card["draft"]["at"].startswith("2026-10-31"), card and card["draft"])
tom = next((r for r in view["replies"] if r["id"] == "Answer Tom"), {})
check("a [slot] draft is marked needs_slot", (tom.get("draft") or {}).get("needs_slot") is True, tom)

code, j = act(id="Reply to Quinn", action="mailprep", mail={})
check("opening the sheet shows her address, cc and the thread",
      code == 200 and j["mail"]["to"] == [SARAH] and j["mail"]["cc"] == ["bob@example.com"]
      and j["mail"]["thread"] and j["mail"]["acct"] == ME and j["mail"]["acct_why"] == "thread", j)
check("the sheet gets the history and the other mail with her",
      len(j["mail"]["history"]) == 1 and [r["subj"] for r in j["mail"]["related"]] == ["Old trip"], j["mail"])
check("opening the sheet keeps nothing pending", not mailer._pending)

code, j = act(id="Reply to Quinn", action="sendmail", mail={}, test=True)
check("test mode says who it would send to", code == 200 and j["msg"].startswith(f"Test mode: would send to {SARAH}"), j)
check("test mode sends nothing", not gmail["sent"] and not mailer._pending)
check("test mode writes no note", NOTE.read_text() == NOTE_TEXT)

code, j = act(id="Answer Tom", action="sendmail", mail={})
check("a [slot] draft is refused", code == 400 and "slot" in j["error"] and not gmail["sent"], j)
code, j = act(id="Answer Tom", action="sendmail", mail={"body": "How about Tuesday 7pm?", "acct": ME}, test=True)
check("with the slot replaced it would go", code == 200 and "would send to tom@example.com" in j["msg"], j)
code, j = act(id="Text Lu", action="sendmail", mail={})
check("WhatsApp is never sent", code == 400 and not gmail["sent"], j)

gmail["late"] = True
code, j = act(id="Reply to Quinn", action="sendmail", mail={})
check("a reply since the draft holds it", code == 200 and j.get("held") and not gmail["sent"], j)
code, j = act(id="Reply to Quinn", action="sendmail", mail={"body": "Friday works too.", "thread_id": "t1"}, force=True)
gmail["late"] = False
check("Send anyway sends, live", code == 200 and j["msg"] == f"Sent to {SARAH}" and len(gmail["sent"]) == 1, j)
acct, body = gmail["sent"].pop()
check("the live send is the edited text in the thread",
      body["threadId"] == "t1" and decoded(body).get_payload(decode=True).decode().strip() == "Friday works too.")
text = NOTE.read_text()
check("the task is closed with a sent Log line and its Next ticked",
      "status: done" in text and "- [x] Next: reply to Quinn" in text
      and f"· sent mail to {SARAH} · check-in app" in text, text)
sig = [json.loads(l) for l in app.LOG.read_text().splitlines()]
sent = [e for e in sig if e.get("signal") == "reply_sent" and not e.get("test")]
check("reply_sent says he edited, with the edit", sent and sent[-1]["edited"] is True
      and sent[-1]["edit"]["body"] == {"draft": "Tuesday 3pm works.", "sent": "Friday works too."}, sent[-1:])
check("the send is logged with Gmail's ids", any(e["kind"] == "mail_sent" and e["gmail_id"] == "g1" for e in sig))
dry = [e for e in sig if e.get("signal") == "reply_sent" and e.get("test")]
check("test-mode send is a signal marked test, unedited", dry and dry[0]["edited"] is False, dry[:1])

code, j = act(id="Text Lu", action="mark", signal="reply_copied", text="hey!! saturday works")
cp = [json.loads(l) for l in app.LOG.read_text().splitlines() if '"reply_copied"' in l]
check("Copy logs reply_copied with his edit", code == 200 and cp and cp[-1]["edited"] is True
      and cp[-1]["edit"] == {"draft": "hey!", "sent": "hey!! saturday works"} and cp[-1]["via"] == "whatsapp", cp[-1:])
act(id="Text Lu", action="mark", signal="reply_copied", text="hey!")
cp = [json.loads(l) for l in app.LOG.read_text().splitlines() if '"reply_copied"' in l]
check("an unedited copy says so", cp[-1]["edited"] is False and "edit" not in cp[-1], cp[-1:])

view = client.get(f"{BASE}/api/view").json()
check("the sent row leaves Replies", not any(r["id"] == "Reply to Quinn" for r in view["replies"]))

print("\nFAILED:\n  " + "\n  ".join(check.fails) if check.fails else "\nall passed")
sys.exit(1 if check.fails else 0)
