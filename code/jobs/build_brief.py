"""Monday 14:00 PT: draft the week's build brief from the #build backlog, then push.

the owner captures build ideas all week (Raycast "Build backlog" checkbox, the phone
Shortcut's "Build backlog" item). They land in `Inbox/` as `type: idea` with
`#build` as the first body line and never get a date, so they stay out of the
cockpit. Monday 18:30-21:00 is the standing build block (after Best Year review
and Maintenance, the owner 2026-10-12). This job turns the
backlog into one plan for that block and has it waiting by 15:00.

## What the brief is

One note, which is both the task and the record (owner decision, 2026-09-30): a
`type: action` in `Inbox/`, scheduled for today, tagged `build-brief`, so it shows
in the ToDo cockpit this afternoon and in the "Build briefs" view of `Inbox.base`
forever after. It closes with `status: done` like any task.

## What the model does, and what Python does

The model reads the backlog and the vault (read-only, `runner.ask`) and writes the
body: already-built checks, patterns and dependencies, an AI-assisted estimate and
a short implementation proposal per item, a session plan with a cut line, and a
discard list. Python writes the frontmatter and the `## Next action` block itself,
so the task schema cannot drift, and refuses an answer that drops an item.

`~/automation` is outside the vault, where the model's Read tool does not reach,
so Python hands it the repo's file list and git log as evidence for the
already-built check. Items are never closed by this job: it proposes, the owner
closes.

    ssh vault-host '~/automation/bin/run-job build_brief --dry-run'
"""

import re
import subprocess
from datetime import date, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from runner import config
from runner.ask import AskFailed, ask
from runner.job import IMPORTANT, ROUTINE, YOU, Job, log
from runner.notify import PushUnavailable, send_push

OBSIDIAN_VAULT = "<VAULT_NAME>"
AUTOMATION = config.HOME / "automation"
BRIEF_TAG = "build-brief"
COVERS = "build-backlog-brief"
# The box runs on UTC; captures everywhere else are the owner's local time.
TZ = ZoneInfo("America/Los_Angeles")

ASK_TIMEOUT = 1500
MAX_ITEM_CHARS = 2500
# Stay in the backlog, but past this age the brief must weigh discarding
# (owner decision, 2026-09-30: nothing is dropped automatically).
STALE_DAYS = 28

FENCE = re.compile(r"^```.*?^```", re.M | re.S)
SPAN = re.compile(r"`[^`\n]*`")
# Bare #build only. `#build-brief` and `#build/x` are different tags, and a
# backticked `#build` is prose about the tag - the same rule as core_learnings.
TAG = re.compile(r"(?:^|\s)#build(?![\w/-])")
FRONT = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)

REQUIRED = ("## Session plan", "## Items", "## Patterns and dependencies",
            "## Already built", "## Propose to discard")
# The three steps the owner works through. Python lifts these sections out of the
# body and renders them at the top as numbered steps, each saying what a tick
# means and leaving a line for his answer (owner decision, 2026-10-05, after the first
# brief: the questions were unclear and the cut line did not say how to proceed).
STEP_BUILT = ("## Step 1: Mark what is already built\n"
              "**Tick = yes, it is built, close it.** If it is not, leave it "
              "unticked and write what is missing.\n\n")
STEP_DISCARD = ("## Step 2: Drop or move what no longer belongs in the backlog\n"
                "**Tick = yes, do it.** To keep an item in the backlog, leave it "
                "unticked and write why.\n\n")
STEP_PLAN = ("## Step 3: Check tonight's build order\n"
             "Each **Tonight** item gets its own session, started in parallel in "
             "plan mode, about 2 h in all; **If time remains** items get one only "
             "when you ask. **To change the plan, "
             "move lines between the two lists or reorder them.** Nothing to tick. "
             "Anything not built tonight comes back next Monday.\n\n")
# What a session does with the answers. Lives in the brief itself, so whichever
# session processes it reads the contract from the note it is processing.
PROCESSING = (
    "## What happens when you say \"process build brief\"\n"
    "1. Every ticked item in step 1 is set to `status: done`. Every unticked one "
    "stays open, with your \"missing\" note added to it.\n"
    "2. Every ticked item in step 2 is dropped (`status: cancelled`), merged, or "
    "moved as its verb says. Every unticked one stays, with your \"keep because\" "
    "reason added to the note so next Monday's brief does not propose it again "
    "without a new reason.\n"
    "3. Claude starts one new session per item to be built, all in parallel, each "
    "in plan mode: **Tonight** first, **If time remains** only when you ask. "
    "Each session plans, asks its open question, builds after your approval and "
    "closes its idea when it ships.\n"
    "4. Once the sessions are started, this brief is set to `status: done`.\n\n")
CUT = "--- cut line ---"
LINK_LINE = re.compile(r"^(?P<link>\[\[[^\]]+\]\]):\s*(?P<text>.+)$")
DISCARD_LINE = re.compile(r"^(?P<verb>[^\[:]+?):\s*(?P<link>\[\[[^\]]+\]\]):\s*(?P<text>.+)$")
MAX_DESCRIPTION_CHARS = 1200
CHECKBOX = re.compile(r"^\s*[-*] \[[ xX]\]")

PROMPT = """\
You are drafting the owner's build brief for tonight. Monday 18:30-21:00 is his
standing build block: vibe coding, agents, automations, prototypes, anything he
builds with Claude Code. It follows a 30 minute Best Year review and an hour of
Maintenance (upkeep of existing docs and automations, not new builds). Dinner
sits inside the build block and 21:00 is a hard stop. So plan about 2 hours of
building, never more than 2.5.

Below are the {n} open item(s) in his build backlog, each a note in `Inbox/`.
Your job is to turn them into a plan he can open at 18:30 and execute top to
bottom in one Claude Code session.

You can Read, Glob and Grep the vault (your working directory). Use them. The
productivity stack is documented in `_system/`: `_system/SYSTEM.md` has a
components table, every component has a doc there, skills live in
`_system/skills/`, and `_system/maintenance/maintenance.md` lists what runs
unattended. Code lives in `~/automation`, which you cannot read; its file list
and recent git history are given at the bottom instead.

For EVERY item, work out:

1. **Already built?** He often builds something and never closes the idea. Check
   the docs, skills and the automation evidence. Say "built", "partly built" or
   "not built", with the evidence (a file, a doc section, a commit line). A built
   item goes in "Already built" as a proposal to close; it is not planned.
2. **Estimate:** wall-clock time for the owner to build it with Claude Code doing
   the typing, including testing and the doc updates his vault rules require
   (component doc, maintenance registration for anything unattended). Use
   15 min / 30 min / 1 h / 2 h / half a day / more. Be honest; a vague idea gets
   a wide range and says why.
3. **Proposal:** 2 to 4 sentences on how to implement it: the approach, which
   existing files or components it touches, and what to reuse. Name real paths
   you found, never invented ones.
4. **Dependencies:** which other items it needs first, or unlocks.

Then:

- **Patterns:** items that are really one build, or share a component, get
  grouped and planned together. Say so explicitly.
- **Priority:** `4-high` items go first unless a dependency forces otherwise.
- **Session plan:** an ordered, numbered list that fills about 2 hours, then a
  line reading exactly `--- cut line ---`, then what would come next if time
  remains, numbering continued. Each line is `N. [[LINK]]: <estimate>`, with no
  running totals. If an item has a question he must answer before it can be
  built, add it to that item's line in one short sentence; nothing else goes in
  this section. Order by dependencies, then priority, then fewest context
  switches.
- **Discard:** propose items to drop, or to move out of the build backlog
  because they are not build work: duplicates, ideas overtaken by something
  built since, and anything so vague or old that it keeps being skipped. List
  ONLY items you propose to drop or move; an item you would keep does not
  belong in this section. Items marked STALE are over {stale} days old: weigh
  each one, and if you keep it, say why in one line in its Items entry.
  Nothing is dropped without his word; you only propose. If an item's note
  carries a "Keep because:" line from an earlier brief, he already refused to
  drop it: propose it again only if something has changed since, and say what.

Write in English. Plain and direct: no em dashes, no filler, no praise.

Refer to every item by its wikilink exactly as given in LINK, e.g. [[Some note]].
Every item must be linked at least once.

Output markdown with exactly these five sections, in this order, and nothing
before the first one:

## Session plan
## Items
One `### [[LINK]]` subsection per item, heading exactly that and nothing else
on the line. Do NOT restate the item's description: Python inserts the captured
text under each heading. Under it write priority, built status, estimate,
proposal and dependencies. **Order the subsections:** first the session-plan
items in plan order, then the items after the cut line in that order, then the
remaining not-built items, then the items in Already built, then the items in
Propose to discard.
## Patterns and dependencies
## Already built
One line per item: `- [[LINK]]: <the evidence, one sentence>`. Write `None.`
if there are none.
## Propose to discard
One line per item: `- <Verb>: [[LINK]]: <why, one or two sentences>`. The verb
says what happens to the item, in a few words and with no colon or link in it:
`Drop`, `Merge into <short name of the other item>`, `Move to the master plan`,
`Make a normal task`, or `Move to <where>`. Write `None.` if there are none.

These two lists are what he reads first, so write them in plain language he
understands without opening a file: no file paths, no backticks, no code names
where words will do. The Items entries carry the detail.

No preamble, no frontmatter, no code fence around the whole answer.

{items}

--- AUTOMATION EVIDENCE (~/automation) ---

Files (git ls-files):
{files}

Commits since {since}:
{commits}
"""


def main() -> int:
    with Job("build_brief", description="Monday build brief",
             watches=(COVERS,)) as job:
        today = datetime.now(TZ).date()
        brief = config.INBOX / f"Build brief {today.isoformat()}.md"
        if brief.exists():
            # A rerun on the same Monday must never produce a second brief.
            job.check("build brief: already drafted today", ok=True,
                      tier=ROUTINE, owner=YOU, covers=COVERS)
            return 0

        items = open_items()
        stale = [i for i in items if i["age"] is not None and i["age"] > STALE_DAYS]
        job.record("open build items", len(items))
        job.record("stale build items", len(stale))

        if not items:
            _push(job, "Build backlog is empty",
                  "Nothing captured with Build backlog since the last brief. "
                  "Tonight's block is free for whatever you pick.", url=None)
            job.check("build brief: backlog empty, nothing to draft", ok=True,
                      tier=ROUTINE, owner=YOU, covers=COVERS)
            return 0

        try:
            body = _no_em_dash(_strip_fence(ask(_prompt(items), timeout=ASK_TIMEOUT)))
            _guard(body, items)
        except AskFailed as exc:
            _push(job, "Build brief failed",
                  f"Could not draft tonight's brief ({str(exc)[:200]}). "
                  f"{len(items)} idea(s) are waiting; run it in a session.", url=None,
                  priority=1)
            job.check("build brief drafted", ok=False, tier=IMPORTANT, owner=YOU,
                      covers=COVERS,
                      detail=f"could not draft: {exc}. The backlog is untouched.")
            return 0

        note = _note(today, body, items)
        if job.dry_run:
            log("build_brief: would write ---------------------------------")
            for line in note.splitlines():
                log(f"  {line}")
            log("build_brief: ------------------------------------------------")
        else:
            brief.write_text(note, encoding="utf-8")
            _push(job, "Build brief ready",
                  f"{len(items)} idea(s) planned for tonight"
                  + (f", {len(stale)} flagged as stale" if stale else "")
                  + ". Three short steps to answer before 17:00.",
                  url=_obsidian_url(f"Inbox/{brief.stem}"))

        job.check("build brief drafted", ok=True, tier=IMPORTANT, owner=YOU,
                  covers=COVERS,
                  detail=f"{brief.name}: {len(items)} item(s)"
                         + (" (dry run, not written)" if job.dry_run else ""))
    return 0


# --- the backlog --------------------------------------------------------------

def open_items() -> list[dict]:
    """Every note in Inbox/ tagged `build`, frontmatter or bare inline, not done.

    Inbox only: captures land there and so does anything commit-to-action makes
    out of a dump. Gating on `type` would be wrong, because a task can be both
    an action and a build item (the pre-2026-09-30 rule made them actions).
    """
    items = []
    for path in sorted(config.INBOX.glob("*.md")):
        if path.name.startswith("_"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        m = FRONT.match(text)
        front, body = (m.group(1), m.group(2)) if m else ("", text)
        fields = _fields(front)
        if fields.get("status") in ("done", "cancelled"):
            continue
        tags = _tags(front)
        if BRIEF_TAG in tags:
            continue
        prose = SPAN.sub("", FENCE.sub("", body))
        if "build" not in tags and not TAG.search(prose):
            continue
        captured = _date(fields.get("captured", ""))
        items.append({
            "path": path,
            "link": path.stem,
            "priority": fields.get("priority", "") or "unset",
            "type": fields.get("type", "") or "untriaged",
            "captured": captured,
            "age": (date.today() - captured).days if captured else None,
            "body": body.strip(),
        })
    return items


def _fields(front: str) -> dict:
    out = {}
    for line in front.splitlines():
        m = re.match(r"^([A-Za-z_]+):\s*(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"\'')
    return out


def _tags(front: str) -> set[str]:
    """`tags:` as a YAML list or inline array. Enough for what captures write."""
    tags, inside = set(), False
    for line in front.splitlines():
        if re.match(r"^tags:\s*\[", line):
            tags |= {t.strip(" '\"#") for t in line.split("[", 1)[1].rstrip("]").split(",")}
            inside = False
        elif re.match(r"^tags:\s*$", line):
            inside = True
        elif inside and re.match(r"^\s+-\s+", line):
            tags.add(line.split("-", 1)[1].strip(" '\"#"))
        elif inside:
            inside = False
    return {t for t in tags if t}


def _date(value: str):
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


# --- the model ------------------------------------------------------------------

def _prompt(items) -> str:
    blocks = []
    for i, it in enumerate(items, 1):
        body = TAG.sub("", it["body"], count=1).strip()
        if len(body) > MAX_ITEM_CHARS:
            body = body[:MAX_ITEM_CHARS] + "\n[... cut; Read the note for the rest]"
        age = "unknown" if it["age"] is None else f"{it['age']} days"
        stale = " STALE" if it["age"] is not None and it["age"] > STALE_DAYS else ""
        blocks.append(f"--- ITEM {i}{stale} ---\nLINK: {it['link']}\n"
                      f"PRIORITY: {it['priority']}\nTYPE: {it['type']}\n"
                      f"CAPTURED: {it['captured'] or 'unknown'} ({age} ago)\n\n{body}")

    oldest = min((it["captured"] for it in items if it["captured"]), default=None)
    since = (oldest or date.today()) - timedelta(days=7)
    return PROMPT.format(n=len(items), stale=STALE_DAYS, items="\n\n".join(blocks),
                         files=_git("ls-files"), since=since.isoformat(),
                         commits=_git("log", f"--since={since.isoformat()}",
                                      "--date=short", "--pretty=%ad %s", "-n", "300"))


def _git(*args) -> str:
    try:
        r = subprocess.run(["git", "-C", str(AUTOMATION), *args],
                           capture_output=True, text=True, timeout=30)
        return r.stdout.strip() or "(none)"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"(unavailable: {exc})"


def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[^\n]*\n", "", t)
        t = re.sub(r"\n```$", "", t)
    return t.strip()


def _no_em_dash(text: str) -> str:
    """The owner's rule is no em dashes. The prompt says so and the model ignored it
    on the first dry run (2026-09-30), so it is enforced here instead."""
    return text.replace(" \u2014 ", ": ").replace("\u2014", ", ")


def _guard(body: str, items) -> None:
    """Refuse an answer that misunderstood the task. Raises AskFailed.

    Only structure is checkable. Whether an estimate is right is not, which is
    what the owner reading the brief before 17:00 is for.
    """
    if not body.startswith("## Session plan"):
        raise AskFailed(f"answer opens {body[:80]!r}, not with '## Session plan'")
    positions = [body.find(h) for h in REQUIRED]
    missing = [h for h, p in zip(REQUIRED, positions) if p < 0]
    if missing:
        raise AskFailed(f"missing section(s): {', '.join(missing)}")
    if positions != sorted(positions):
        raise AskFailed("sections out of order")
    if "--- cut line ---" not in body:
        raise AskFailed("session plan has no cut line")
    # An item that silently vanishes is the one failure nobody would notice:
    # the brief would read complete and the idea would sit unread forever.
    dropped = [it["link"] for it in items if f"### [[{it['link']}" not in body]
    if dropped:
        raise AskFailed(f"{len(dropped)} item(s) have no ### heading in Items: "
                        + "; ".join(d[:40] for d in dropped))


def _note(today: date, body: str, items) -> str:
    """Frontmatter, the Next action block and the three steps are Python's.

    The model writes the body; Python moves its Already built list, discard list
    and session plan to the top as steps 1 to 3, and puts each item's own
    captured text under its heading, so a cut-off filename never leaves an item
    without its description.
    """
    now = datetime.now(TZ)
    high = sum(it["priority"] == "4-high" for it in items)
    body, steps = _lift_steps(body)
    body = _insert_descriptions(body, items)
    return (
        "---\n"
        "type: action\n"
        f"captured: {now.strftime('%Y-%m-%dT%H:%M')}\n"
        "status: open\n"
        "priority: 3-normal\n"
        f"scheduled: {today.isoformat()}\n"
        "startable: cold\n"
        "tags:\n"
        f"  - {BRIEF_TAG}\n"
        "---\n\n"
        "## Next action\n"
        "- [ ] Next: work through the steps below (about 5 minutes)\n"
        f"- [ ] Then: open Claude Code and say \"process build brief {today.isoformat()}\"\n\n"
        + steps
        + PROCESSING
        + f"*Drafted by `build_brief` on vault-host, {now.strftime('%Y-%m-%d %H:%M %Z')}, "
        f"from {len(items)} open `#build` item(s), {high} high priority. Nothing was "
        "closed or discarded; every change is yours to make.*\n\n"
        + body.strip() + "\n"
    )


def _section(body: str, heading: str) -> tuple[int, int]:
    """Span of a `## ` section, heading included, up to the next `## ` or the end."""
    start = body.index(heading)
    nxt = body.find("\n## ", start + len(heading))
    return start, (nxt + 1 if nxt >= 0 else len(body))


def _lift(body: str, heading: str) -> tuple[str, list[str]]:
    """Remove a section from the body; return the body and the section's lines."""
    start, end = _section(body, heading)
    lines = [l.rstrip() for l in body[start:end].splitlines()[1:]]
    return body[:start] + body[end:], lines


def _entries(lines) -> list[str]:
    return [re.sub(r"^[-*]\s+", "", l.strip()) for l in lines
            if re.match(r"^\s*[-*]\s+\S", l)]


def _lift_steps(body: str) -> tuple[str, str]:
    """Steps 1 to 3 from the model's Already built, discard and plan sections.

    Only the title goes on the checkbox line, so a tick strikes through the title
    and not the reasoning, and every item gets an empty answer line. A line the
    model wrote in the wrong shape is kept as it is rather than lost.
    """
    top = []
    body, lines = _lift(body, "## Already built")
    out = []
    for e in _entries(lines):
        m = LINK_LINE.match(e)
        out += ([f"- [ ] {m['link']}", f"    - Evidence: {m['text']}"] if m
                else [f"- [ ] {e}"]) + ["    - Missing:"]
    if out:
        top.append(STEP_BUILT + "\n".join(out) + "\n\n")

    body, lines = _lift(body, "## Propose to discard")
    out = []
    for e in _entries(lines):
        m = DISCARD_LINE.match(e)
        out += ([f"- [ ] **{m['verb'].strip()}:** {m['link']}", f"    - Why: {m['text']}"] if m
                else [f"- [ ] {e}"]) + ["    - Keep because:"]
    if out:
        top.append(STEP_DISCARD + "\n".join(out) + "\n\n")

    body, lines = _lift(body, "## Session plan")
    text = "\n".join(lines).strip()
    tonight, _, later = text.partition(CUT)
    plan = "**Tonight**\n" + tonight.strip() + "\n"
    if later.strip():
        plan += "\n**If time remains**\n" + later.strip() + "\n"
    top.append(STEP_PLAN + plan + "\n")
    return body, "".join(top)


def _description(item) -> str:
    """The captured text, minus the #build tag line and any task checkboxes, which
    belong to the note and would read as brief steps here."""
    text = TAG.sub("", item["body"], count=1)
    lines = [l for l in text.splitlines()
             if not CHECKBOX.match(l) and not re.match(r"^#+\s*Next action", l)]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if len(text) > MAX_DESCRIPTION_CHARS:
        text = text[:MAX_DESCRIPTION_CHARS].rsplit(" ", 1)[0] + f" ... [[{item['link']}|rest in the note]]"
    return "\n".join("> " + l if l else ">" for l in text.splitlines()) or "> (no description captured)"


def _insert_descriptions(body: str, items) -> str:
    for it in items:
        m = re.search(r"^### \[\[" + re.escape(it["link"]) + r"[^\n]*\n", body, re.M)
        if m:
            body = body[:m.end()] + _description(it) + "\n\n" + body[m.end():]
    return body


# --- delivery -------------------------------------------------------------------

def _obsidian_url(vault_path: str) -> str:
    return ("obsidian://open?vault=" + quote(OBSIDIAN_VAULT)
            + "&file=" + quote(vault_path, safe=""))


def _push(job: Job, title: str, message: str, *, url: str | None,
          priority: int = 0) -> None:
    if job.dry_run:
        log(f"build_brief: would push {title!r}: {message}")
        return
    try:
        send_push(title, message, url=url, url_title="Open brief" if url else None,
                  priority=priority)
    except PushUnavailable as exc:
        # The brief exists either way; losing the push only costs the nudge,
        # but that nudge is the point at 15:00, so it is never silent.
        job.check("build brief push delivered", ok=False, tier=IMPORTANT, owner=YOU,
                  covers=COVERS, detail=str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
