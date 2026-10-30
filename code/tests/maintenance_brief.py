#!/usr/bin/env python3
"""The Monday maintenance brief: what it lists, how, and who watches it.

    ~/automation/.venv/bin/python -m tests.maintenance_brief

  1. "Still broken" is built mechanically from state and the log. A finding
     the model leaves out of "Needs you" still appears, dated from the log,
     and a resolved block restarts the count.
  2. The model's answer is parsed strictly: missing fields are refused, more
     than five items are trimmed, preamble is refused.
  3. The note: only registered frontmatter keys, five sections in order, a
     "Create a task?" per item, a machine-readable block with ids, no em dash.
  4. The whole job, with the model stubbed: repairs run first, the note lands,
     it goes to Telegram (one push if Telegram is down), a second run the same day writes nothing. A failed
     model pass still writes the mechanical brief.
  5. heartbeat's CRITICAL check that this week's brief ran.
"""
import json
import pathlib
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from runner import config

TMP = pathlib.Path(tempfile.mkdtemp())
config.VAULT = TMP / "vault"
config.INBOX = config.VAULT / "Inbox"
config.INBOX.mkdir(parents=True)
(config.VAULT / "_system" / "maintenance").mkdir(parents=True)
config.STATE_DIR = TMP / "state"
config.LOCK_DIR = config.STATE_DIR / "locks"
config.ENV_FILE = TMP / "no-such-env"
config.HOME = TMP / "home"
config.HOME.mkdir()
config.ensure_dirs()

(config.VAULT / "_system" / "maintenance" / "maintenance.md").write_text("""\
### 1. Only you can do these

| What | When | Why it matters |
|---|---|---|
| Canvas access token <!--[canvas-token]--> | expires %s | archive goes quiet |
| Gmail app password <!--[gmail-app-password]--> | if revoked | alerts |

### 2. Nothing can check these
""" % (date.today() + timedelta(days=12)).isoformat())

from runner import alert_log, notify, repairs
from runner.alert_log import Entry
from jobs import maintenance_brief as B
from jobs import heartbeat as H
from runner.job import Job

fails = []


def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if not ok and detail else ""))
    if not ok:
        fails.append(label)


NOW = datetime.now(timezone.utc)


def st(job, status="problem", checks=(), age_days=0.5, scheduled=True, **kw):
    return {"job": job, "status": status, "checks": list(checks), "_age_days": age_days,
            "_scheduled": scheduled,
            "finished": (NOW - timedelta(days=age_days)).isoformat(), **kw}


def chk(name, tier="important", owner="you", detail="d", streak=1, key=None):
    return {"name": name, "ok": False, "tier": tier, "owner": owner, "detail": detail,
            "streak": streak, "key": key or name}


# --- 1. still broken, mechanically --------------------------------------------
states = [
    st("heartbeat", checks=[chk("timers committed", streak=9),
                            {"name": "sync up", "ok": True, "key": "sync up"}]),
    st("tripwires", checks=[chk("North Star due for review", key="revisit|ns")]),
    st("wiki", status="error", note="KeyError: 'x'", checks=[]),
    st("vaultmap", status="ok", age_days=12),
    st("selftest", status="ok", age_days=12, scheduled=False),
    st("maintenance_brief", checks=[chk("last week's own finding")]),
]
history = [
    Entry(job="tripwires", check="North Star due for review", key="revisit|ns", tier="important",
          event="failing", time=NOW - timedelta(days=30)),
    Entry(job="tripwires", check="North Star due for review", key="revisit|ns", tier="important",
          event="resolved", time=NOW - timedelta(days=20)),
    Entry(job="tripwires", check="North Star due for review", key="revisit|ns", tier="important",
          event="failing", time=NOW - timedelta(days=10)),
    Entry(job="wiki", check="job crashed or could not start", key="crash|wiki", tier="important",
          event="crashed", time=NOW - timedelta(days=2)),
]
drift = [("keys-extra|~/.waha.env", "`~/.waha.env` holds key(s) the doc does not list: X_Y")]
broken = B.still_broken(states, history, drift, NOW)
names = [i["check"] for i in broken]
check("every failing check in every job's state is listed",
      "timers committed" in names and "North Star due for review" in names, str(names))
check("a crashed job is listed", "job crashed on its last run" in names, str(names))
check("a scheduled job that stopped running is listed",
      any(i["check"] == "job stopped running" and i["job"] == "vaultmap" for i in broken))
check("a manual job with an old report is not called stopped",
      not any(i["job"] == "selftest" for i in broken))
check("passing checks are not listed", "sync up" not in names)
check("last week's brief state is not re-listed", "last week's own finding" not in names)
check("credentials drift found this run is listed",
      any(i["check"].startswith("credentials drift:") for i in broken))
ns = next(i for i in broken if i["check"] == "North Star due for review")
check("weeks are counted from the log, restarting after a resolved block",
      ns["age"].startswith("broken 2 weeks"), ns["age"])
hb = next(i for i in broken if i["check"] == "timers committed")
check("a finding older than the log falls back to its streak",
      "9 runs" in hb["age"] and "before the log" in hb["age"], hb["age"])
check("crashes sort ahead of important findings",
      names.index("job crashed on its last run") < names.index("timers committed"), str(names))

# --- 2. parsing the model's answer ----------------------------------------------
ITEM = """### {n}. Thing {n}
- What is wrong: broken {n}
- Why it matters: matters {n}
- Next action: run `vt docs`
- Effort: 15 min
"""
good = "## Needs you\n\n" + ITEM.format(n=1) + "\n" + ITEM.format(n=2) + \
       "\n## Expiring within 30 days\n- Canvas token (in 12 days): archive stops\n"
items, exp = B.parse_answer(good)
check("a well-formed answer parses into items", [i["title"] for i in items] == ["Thing 1", "Thing 2"],
      str(items))
check("...with all four fields", all(i["Next action"] == "run `vt docs`" for i in items))
check("...and the expiring section", exp.startswith("- Canvas token"), exp)

seven = "## Needs you\n" + "".join(ITEM.format(n=n) for n in range(1, 8)) + \
        "## Expiring within 30 days\nNone.\n"
check(f"more than {B.MAX_NEEDS_YOU} items are trimmed to {B.MAX_NEEDS_YOU}",
      len(B.parse_answer(seven)[0]) == B.MAX_NEEDS_YOU)

items, _ = B.parse_answer("## Needs you\nNothing needs you this week.\n\n"
                          "## Expiring within 30 days\nNone.\n")
check("'Nothing needs you this week' is a valid answer", items == [])


def refused(text):
    try:
        B.parse_answer(text)
        return False
    except B.AskFailed:
        return True


pre, _ = B.parse_answer("Checked the vault. Writing the brief now.\n\n" + good)
check("preamble before '## Needs you' is dropped, the items still parse", len(pre) == 2, str(pre))
check("an answer with no '## Needs you' heading is refused",
      refused("Sure! Here it is:\n" + good.replace("## Needs you", "## Needs")))
check("a missing Expiring section is refused", refused(good.split("## Expiring")[0]))
check("an item missing a field is refused",
      refused(good.replace("- Effort: 15 min\n", "", 1)))
check("an empty Needs you that does not say so is refused",
      refused("## Needs you\n\n## Expiring within 30 days\nNone.\n"))

# --- 3. the note -------------------------------------------------------------------
items, exp = B.parse_answer(good)
fixed, tried = B.fixed_this_week(
    [Entry(job="heartbeat", check="sync up", key="sync up", tier="critical", event="resolved",
           time=NOW - timedelta(days=1))],
    [repairs.Action("restart", "waha", why="waha was not active", outcome="fixed",
                    detail="restarted"),
     repairs.Action("restart", "wa-consumer", why="wa-consumer was not active",
                    outcome="not fixed", detail="still down")])
check("a repair that worked is under Fixed", any("waha" in f for f in fixed), str(fixed))
check("a check that passes again is under Fixed", any("sync up" in f for f in fixed), str(fixed))
check("a repair that failed is under Tried, not Fixed",
      any("wa-consumer" in t for t in tried) and not any("wa-consumer" in f for f in fixed))

ran = B.everything_else(states, [], NOW)
note = B.render(date.today(), items, exp, fixed, tried, broken, ran, "",
                datetime.now(B.TZ))
front = note.split("\n---\n", 1)[0].splitlines()[1:]
keys = {l.split(":", 1)[0] for l in front}
check("frontmatter uses only registered keys (type, component, last_run)",
      keys == {"type", "component", "last_run"}, str(keys))
check("no status:, stage: or verified: (a record, not a task or a doc)",
      not keys & {"status", "stage", "verified", "captured", "revisit"})
heads = [l for l in note.splitlines() if l.startswith("## ")]
check("the five sections, in order, then the items block",
      heads == ["## 1. Needs you", "## 2. Fixed automatically this week", "## 3. Still broken",
                "## 4. Expiring within 30 days", "## 5. Everything else ran",
                "## Items, machine-readable"], str(heads))
check("every Needs you item asks 'Create a task?'",
      note.count("**Create a task?**") == len(items) == 2)
block = note.split("```maintenance-brief-items\n", 1)[1].split("\n```", 1)[0]
data = json.loads(block)
check("the machine block lists the items with stable ids",
      [i["id"] for i in data["items"]] == [f"mb-{date.today()}-1", f"mb-{date.today()}-2"]
      and data["items"][0]["next_action"] == "run `vt docs`", block)
sec3 = note.split("## 3. Still broken", 1)[1].split("## 4.", 1)[0]
for i in broken:
    check(f"Still broken carries '{i['check'][:40]}'", i["check"] in sec3)
check("section 5 is one line", len([l for l in note.split("## 5. Everything else ran", 1)[1]
                                     .split("## Items", 1)[0].splitlines() if l.strip()]) == 1)
check("no em dash anywhere in the note", "—" not in note)
check("the model's em dashes are removed", "—" not in B._no_em_dash("a — b—c"))

# --- 4. the whole job -----------------------------------------------------------------
pushes, mails, asked, grams = [], [], [], []
notify.send_push = lambda title, message, **kw: pushes.append((title, message, kw))
# Never the real bot: on the box ~/.automation.env holds a live token.
notify.send_telegram = lambda text, **kw: grams.append((text, kw)) or len(grams)
notify.send_mail = lambda s, b: mails.append((s, b)) or "to@example.com"
B._systemctl = lambda *a: "enabled" if a[0] == "is-enabled" else "inactive"
B._on_box = lambda: True      # so the (faked) repair menu runs
B._drift = lambda job: []     # credentials_drift has its own test
order = []


class FakeRepairs(repairs.Repairs):
    def run(self, states):
        order.append("repairs")
        return []


B.repairs.Repairs = FakeRepairs


def fake_ask(prompt, **kw):
    order.append("ask")
    asked.append(prompt)
    return good


B.ask = fake_ask
for s in states:
    (config.STATE_DIR / f"{s['job']}.json").write_text(json.dumps(
        {k: v for k, v in s.items() if not k.startswith("_")}))
alert_log.append([Entry(job="heartbeat", check="obsidian-sync running", tier="critical",
                        event="failing", detail="not active", time=NOW - timedelta(hours=2))])

sys.argv = ["maintenance_brief"]
B.main()
today = datetime.now(B.TZ).date()
path = B.brief_path(today)
check("the brief lands in _system/maintenance/briefs/<date>.md", path.exists(), str(path))
check("repairs run before the model turn", order == ["repairs", "ask"], str(order))
check("the prompt carries the week's alert log", "obsidian-sync running" in asked[0])
check("the prompt carries the mechanical Still broken list", "timers committed" in asked[0])
check("the brief goes to Telegram: a header plus one message per item", len(grams) == 3,
      str(grams))
check("...the header carries the counts and the note path",
      grams and "Maintenance: 2 need you" in grams[0][0] and "briefs/" in grams[0][0], str(grams))
from jobs.tg_bridge import proactive as P
check("...each item has Create task / Skip buttons naming its date and rank",
      [g[1].get("buttons") for g in grams[1:]]
      == [P.brief_buttons(str(today), 1), P.brief_buttons(str(today), 2)], str(grams))
check("...the item text carries its next action, without markdown backticks",
      len(grams) > 1 and "Next: run vt docs" in grams[1][0]
      and not any("`" in g[0] for g in grams), str(grams))
check("...one buzz: the header follows quiet hours, every item is silent",
      grams and grams[0][1].get("silent") is None
      and all(g[1].get("silent") is True for g in grams[1:]), str([g[1] for g in grams]))
check("no push and no mail when Telegram lands", pushes == [] and mails == [],
      f"{pushes} {mails}")
check("no Inbox note is created", list(config.INBOX.glob("*.md")) == [])

pushes.clear()
asked.clear()
grams.clear()
B.main()
check("a second run the same day writes nothing and sends nothing",
      pushes == [] and asked == [] and grams == [], f"{pushes} {asked} {grams}")

path.unlink()
pushes.clear()


def telegram_down(text, **kw):
    raise notify.TelegramUnavailable("Telegram HTTP 502: Bad Gateway")


notify.send_telegram = telegram_down   # the failed-model run below also proves the fallback


def failing_ask(prompt, **kw):
    raise B.AskFailed("no answer within 900s")


B.ask = failing_ask
B.main()
text = path.read_text()
check("a failed model pass still writes the brief", path.exists())
check("...says so in Needs you", "The model pass failed" in text)
check("...and still lists every open finding", "timers committed" in text and "North Star" in text)
check("Telegram down: the brief falls back to one push", len(pushes) == 1, str(pushes))
check("...linking to the note", pushes and "briefs%2F" in pushes[0][2].get("url", ""), str(pushes))
check("...and the push says nothing is ranked", pushes and "nothing is ranked" in pushes[0][1],
      str(pushes))
state = json.loads((config.STATE_DIR / "maintenance_brief.json").read_text())
check("...and the job records the failure as an important check",
      any(c["name"] == "maintenance brief written" and not c["ok"] for c in state["checks"]))

# --- 4b. who does it: needs you, or Claude can fix it (2026-10-04) ---------------------
CLAUDE_ITEM = """### {n}. {title}
- What is wrong: {what}
- Why it matters: m
- Next action: {next}
- Effort: 15 min
- Who: {who}
"""
mixed = ("## Needs you\n\n"
         + CLAUDE_ITEM.format(n=1, title="North Star", what="expired", next="re-form goals", who="you")
         + CLAUDE_ITEM.format(n=2, title="Timer not in repo", what="unit only on the box",
                              next="commit units/automation@routine_push.timer", who="Claude")
         + CLAUDE_ITEM.format(n=3, title="Stray key", what="~/.waha.env has a key",
                              next="edit ~/.waha.env to remove the unused key", who="Claude")
         + CLAUDE_ITEM.format(n=4, title="No Who", what="x", next="y", who="").replace("- Who: \n", "")
         + "## Expiring within 30 days\nNone.\n")
kinds = [i["kind"] for i in B.parse_answer(mixed)[0]]
check("Who: you stays you, Who: Claude becomes a fix item", kinds[:2] == ["you", "claude"], kinds)
check("credentials overrule the model: a Claude item touching .env or credentials is you",
      kinds[2] == "you", kinds)
check("a missing Who is you, never a fix button by default", kinds[3] == "you", kinds)
# Actions, not mentions (option A, the owner 2026-10-04).
for text in ("sudo systemctl restart x", "systemctl --user restart tg-bridge",
             "run systemctl daemon-reload", "copy it to /etc/systemd/system/x.timer",
             "edit ~/.automation.env", "add the key to ~/.waha.env",
             "rotate the API key", "change the bot token", "revoke the old token in BotFather"):
    check(f"always you: {text!r}",
          B.kind_of({"title": "t", "Who": "Claude", "Next action": text}) == "you")
for text in ("commit the unit file",
             "Write the unit text to ~/automation/units/automation@routine_push.timer and diff it "
             "against /etc/systemd/system/automation@routine_push.timer. Commit it to ~/automation.",
             "check systemctl status tg-bridge", "confirm with systemctl is-active waha",
             "add WAHA_API_KEY_SHA512 to the key list in _system/credentials.md",
             "fix the 4 broken wikilinks"):
    check(f"stays Claude: {text[:50]!r}",
          B.kind_of({"title": "t", "Who": "Claude", "Next action": text}) == "claude")

mitems, _ = B.parse_answer(mixed)
mnote = B.render(date.today(), mitems, "None.", [], [], [], "ran", "", datetime.now(B.TZ))
check("the note labels each item", "**Who:** Claude can do it" in mnote and "**Who:** Needs you" in mnote)
check("...and explains both buttons", "Let Claude fix it (it creates the task" in mnote
      and mnote.count("or Create task") == 4)
mblock = json.loads(mnote.split("```maintenance-brief-items\n", 1)[1].split("\n```", 1)[0])
check("the machine block carries each item's kind",
      [i["kind"] for i in mblock["items"]] == ["you", "claude", "you", "you"], mblock)

grams.clear()
notify.send_telegram = lambda text, **kw: grams.append((text, kw)) or len(grams)
B._telegram(date(2026, 10, 5), "s", "m", "_system/maintenance/briefs/2026-10-05", mitems)
from jobs.tg_bridge import proactive as P2
check("Telegram: every item gets Let Claude fix it and Create task",
      all(g[1]["buttons"] == P2.brief_buttons("2026-10-05", n) for n, g in enumerate(grams[1:], 1)),
      str(grams[1:3]))
check("...and the kind only labels the message",
      "Needs your decision" in grams[1][0] and "Claude can likely finish this" in grams[2][0],
      str([g[0][-60:] for g in grams[1:3]]))

# Reopen: a fix Claude closed last week whose finding is still failing.
task = config.INBOX / "Timer not in repo.md"
task.write_text("---\ntype: action\nstatus: done\n---\n\n## Next action\n"
                "- [x] Next: commit the unit\n")
fixes = [{"task": str(task), "title": "Timer not in repo", "summary": "committed",
          "status": "done", "finished": "2026-10-05T15:00"}]
again = B.parse_answer("## Needs you\n\n" + CLAUDE_ITEM.format(
    n=1, title="Timer still not in repo", what="still only on the box", next="commit it",
    who="Claude").replace("- Who: Claude\n", f"- Who: Claude\n- Reopens: Inbox/{task.name}\n")
    + "## Expiring within 30 days\nNone.\n")[0]
B.apply_reopens(again, fixes, dry_run=True)
check("dry run: a reopen is only logged", "status: done" in task.read_text())
B.apply_reopens(again, fixes, dry_run=False)
check("a fix that did not hold reopens its task",
      "status: inbox" in task.read_text() and "- [ ] Next: commit the unit" in task.read_text(),
      task.read_text())
check("...and the item becomes yours, marked reopened",
      again[0]["kind"] == "you" and again[0]["reopened"] == "Timer not in repo", again)
bogus = B.parse_answer("## Needs you\n\n" + ITEM.format(n=1).replace(
    "- Effort: 15 min", "- Effort: 15 min\n- Reopens: Inbox/Some other task.md")
    + "## Expiring within 30 days\nNone.\n")[0]
B.apply_reopens(bogus, fixes, dry_run=False)
check("Reopens naming a task that is not a closed fix is ignored", "reopened" not in bogus[0], bogus)
check("the prompt lists last week's closed fixes",
      "FIXES CLAUDE CLOSED SINCE THE LAST BRIEF" in B._prompt(date.today(), [], [], None, [], [], [],
                                                              fixes)
      and "Timer not in repo: committed" in B._prompt(date.today(), [], [], None, [], [], [], fixes))
task.unlink()
grams.clear()

# --- 5. heartbeat watches the brief -----------------------------------------------------
H._systemctl = lambda *a: "enabled"
H._active_since = lambda unit: None


def hb_check():
    j = Job("hb_probe", description="probe", dry_run=True)
    H._check_maintenance_brief(j)
    c = j.checks[-1]
    return c


now = datetime.now(B.TZ)
due = (now - timedelta(days=now.weekday())).replace(hour=14, minute=5, second=0, microsecond=0)
if due > now - timedelta(hours=H.BRIEF_GRACE_H):
    due -= timedelta(days=7)
for f in B.briefs_dir().glob("*.md"):
    f.unlink()
(config.STATE_DIR / "maintenance_brief.json").unlink()

c = hb_check()
check("heartbeat's brief check is CRITICAL", c.tier == "critical", c.tier)
check("no brief and no state, timer enabled long ago: fails", not c.ok, c.detail)

H._active_since = lambda unit: due.astimezone(timezone.utc) + timedelta(hours=1)
check("a timer installed after the last due time: not yet due, passes", hb_check().ok)

H._systemctl = lambda *a: "disabled"
H._active_since = lambda unit: None
c = hb_check()
check("never run and the timer not enabled: fails, and says so",
      not c.ok and "not enabled" in c.detail, c.detail)

(config.STATE_DIR / "maintenance_brief.json").write_text(json.dumps({
    "job": "maintenance_brief", "status": "problem", "dry_run": True,
    "finished": datetime.now(timezone.utc).isoformat()}))
H._systemctl = lambda *a: "enabled"
H._active_since = lambda unit: due.astimezone(timezone.utc) + timedelta(hours=1)
check("only a dry run so far, timer installed after the due time: still in first-run grace",
      hb_check().ok)
H._active_since = lambda unit: None
(config.STATE_DIR / "maintenance_brief.json").unlink()

B.brief_path(due.date()).parent.mkdir(parents=True, exist_ok=True)
B.brief_path(due.date()).write_text("x")
check("this week's brief note exists: passes", hb_check().ok)
B.brief_path(due.date()).unlink()

(config.STATE_DIR / "maintenance_brief.json").write_text(json.dumps({
    "job": "maintenance_brief", "status": "ok", "dry_run": False,
    "finished": (due + timedelta(minutes=20)).isoformat()}))
check("no note but a real run since the due time: passes", hb_check().ok)

(config.STATE_DIR / "maintenance_brief.json").write_text(json.dumps({
    "job": "maintenance_brief", "status": "ok", "dry_run": True,
    "finished": (due + timedelta(days=1)).isoformat()}))
c = hb_check()
check("a dry run since the due time does not count", not c.ok, c.detail)

(config.STATE_DIR / "maintenance_brief.json").write_text(json.dumps({
    "job": "maintenance_brief", "status": "ok", "dry_run": False,
    "finished": (due - timedelta(days=7)).isoformat()}))
c = hb_check()
check("last week's run only: fails", not c.ok, c.detail)
check("...and the detail names the missing date", str(due.date()) in c.detail, c.detail)

print()
if fails:
    print(f"FAILED: {len(fails)} check(s): {', '.join(fails)}")
    sys.exit(1)
print("all checks passed")
