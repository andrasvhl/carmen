"""Check-in Mini App: the plan-today rows as a Telegram Mini App (MVP, 2026-10-30).

owner decision, Oct 30: the check-in must not be one chained morning run, must be
reachable from phone and laptop whenever, and must live in a single interface
on the phone. So the rows the 08:00 pre-run already writes
(`_system/claude/plan-today-prerun.md`) are served as a Mini App that opens
inside the vault bot's chat. Taps write the task note at once; Talk posts the
row into the chat, where replying continues it with the tg_bridge agent.

Auth, two ways to the same page (owner decision, Oct 30: one system for phone and
desktop, "so can use from chrome? and from claude browser?"):
  - Inside Telegram: the Mini App's initData, signed with the bot token; only
    TELEGRAM_OWNER_ID passes.
  - In a browser: CHECKIN_PASSWORD once, then a 30-day signed cookie
    (CHECKIN_SECRET). Five wrong passwords lock the login for 10 minutes.
The page itself holds no data. Binds 127.0.0.1 only; the Tailscale Funnel path
/checkin proxies it.

Design and open questions: Inbox/Review plan-today after its first week.md.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hmac
import os
import threading
from pathlib import Path

from runner import config

from .adapters import state   # every state file changes through state.update; the view only reads (Nov 6)
from .adapters import clock
from .adapters import taplog
from .adapters import notes
from .adapters import calendar
from .tasks import read as tasks_read
from .tasks import actions as task_actions
from .signals import read as signals_read
from .signals.journal import entry as meditation_entry
from .planning import capacity
from .planning import read as planning_read
from . import runs
from . import talk
from . import outcomes
from . import web


HTML = Path(__file__).with_name("page.html")
PORT = int(os.environ.get("CHECKIN_PORT", "8790"))


# --- the server ----------------------------------------------------------------------

def make_app():
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
    from starlette.routing import Route

    env = config.load_env()
    token, owner = env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_OWNER_ID", "")
    password, secret = env.get("CHECKIN_PASSWORD", ""), env.get("CHECKIN_SECRET", "")
    lockout = web.Lockout()

    def authed(req: Request) -> bool:
        return (web.verify_init_data(req.headers.get("x-init-data", ""), token, owner)
                or web.verify_cookie(req.cookies.get(web.COOKIE, ""), secret))

    async def login(req: Request):
        if req.method == "GET":
            return HTMLResponse(web.LOGIN_HTML.replace("{msg}", ""))
        if lockout.locked():
            return HTMLResponse(web.LOGIN_HTML.replace("{msg}", "<p>Too many tries, wait 10 minutes.</p>"),
                                status_code=429)
        form = await req.form()
        given = str(form.get("password", ""))
        if not password or not secret or not hmac.compare_digest(given.encode(), password.encode()):
            lockout.fail()
            return HTMLResponse(web.LOGIN_HTML.replace("{msg}", "<p>Wrong password.</p>"), status_code=403)
        resp = RedirectResponse("./", status_code=303)
        resp.set_cookie(web.COOKIE, web.make_cookie(secret), max_age=web.COOKIE_AGE, path="/",
                        secure=True, httponly=True, samesite="lax")
        return resp

    def current_view() -> dict:
        try:
            return planning_read.build_view(signals_read.load_prerun())
        except Exception as exc:   # a bad quick update must never take the app down (Oct 31)
            taplog.log_event("error", where="update overlay", detail=str(exc)[:200])
        try:
            return planning_read.build_view(signals_read.load_prerun(overlay=False))
        except Exception as exc:   # nor a bad pre-run: an empty day that says so (Nov 1)
            taplog.log_event("error", where="bare pre-run", detail=str(exc)[:200])
        try:
            v = planning_read.build_view({"rows": [], "generated": "", "notes": []})
        except Exception as exc:
            taplog.log_event("error", where="empty view", detail=str(exc)[:200])
            day = {"one": None, "needs": [], "more": [], "done": [], "waiting": [], "closed": [],
                   "selection": [], "suggest": [], "budget": {"free_min": None, "planned_min": 0, "unestimated": 0,
                                                              "window": "", "busy": [], "day_start": capacity.DAY_START, "day_end": capacity.DAY_END}}
            v = {"generated": "", "updated": "", "update_summary": [], "today": dict(day),
                 "tomorrow": {**day, "date": (clock.carmen_day() + dt.timedelta(days=1)).isoformat()},
                 "prerun": {"running": False, "last_ok": None}, "replies": [], "claude": [],
                 "counts": {"today": 0, "replies": 0, "claude": 0}, "notes": []}
        v["notes"] = ["The morning list could not be read, so only what is safe shows. It is logged; a Refresh or the next pre-run fixes it."] + v["notes"]
        return v

    def find(row_id: str) -> dict | None:
        r = next((r for r in planning_read.all_rows(current_view()) if r["id"] == row_id), None)
        if r is None and row_id and notes.task_path(row_id):
            # Any open task note (Nov 10: Plan's All tasks lists every open task, also ones no
            # list of the view holds, such as a task dated later): a minimal row for the tap.
            r = {"id": row_id, "title": row_id, "task": True, "group": "", "why": "", "next": "", "suggest": "",
                 "guess": "", "prio": "Z", "detail": {}}
        return r

    def card_id(row_id: str, dry: bool = False) -> str:
        """The note name behind a card (Nov 4, cardnotes): a card without a note yet (a live
        meditation row) gets one here, on its first tap; a page still holding the card's title
        finds the note made for it. Test mode never creates."""
        from .tasks import cardnotes
        if not row_id or notes.task_path(row_id):
            return row_id
        s = cardnotes.stem_for(row_id, open_only=True) or cardnotes.existing(row_id) or cardnotes.stem_for(row_id)
        if s:
            return s
        r = None if dry else find(row_id)
        if not r:
            return row_id
        p = cardnotes.create({"n": r.get("title") or row_id, "nx": r.get("next", ""), "s": r.get("why", ""),
                              "ne": r.get("est", ""), "ch": [{"msg": r["draft"]}] if r.get("draft") else []}, "tap")
        if p:
            taplog.log_event("card_note_created", id=row_id, note=p.stem)
        return p.stem if p else row_id

    def source(req: Request) -> str:
        return "telegram" if req.headers.get("x-init-data") else "browser"

    STATIC = {"manifest.webmanifest": "application/manifest+json",
              "icon-180.png": "image/png", "icon-192.png": "image/png", "icon-512.png": "image/png"}

    async def static(req: Request):
        # The installable-app bits (Chrome Install, Safari Add to Dock). No data, no auth.
        name = req.url.path.rsplit("/", 1)[-1]
        return Response((HTML.parent / name).read_bytes(), media_type=STATIC[name],
                        headers={"Cache-Control": "max-age=86400"})

    async def page(req: Request):
        return HTMLResponse(HTML.read_text(encoding="utf-8"),
                            headers={"Cache-Control": "no-store"})

    talk_check = {"at": 0.0}

    async def view(req: Request):
        if not authed(req):
            return JSONResponse({"error": "unauthorised"}, status_code=403)
        # A page load only reads (Nov 6). The talk check, starting prep and the bookkeeping the view
        # used to write run in the server's minute tick (tick() in lifespan).
        v = await asyncio.to_thread(current_view)
        try:
            planning_read.attach_shorts(v)
        except Exception as exc:
            taplog.log_event("error", where="attach_shorts", detail=str(exc)[:200])
        try:
            runs.attach_prep(v)
        except Exception as exc:
            taplog.log_event("error", where="attach_prep", detail=str(exc)[:200])
        try:   # which cards already have a chat in the app: their Talk shows it
            from .chat import summaries
            v["chats"] = summaries()
            v["subticks"] = state._load_json(task_actions.SUBTICKS, {})
            try:
                from .planning import do as _do
                v["wins"] = tasks_read.today_wins(clock.do_day(dt.datetime.now(clock.TZ)))
            except Exception as exc:   # a delight, never a broken view
                taplog.log_event("error", where="today_wins", detail=repr(exc)[:200])
            try:
                v["plan_pending"] = planning_read.plan_pending(clock.carmen_day())
            except Exception as exc:   # a reminder, never a broken view
                taplog.log_event("error", where="plan_pending", detail=repr(exc)[:200])
            try:   # his note for Claude per day (r8 F2): on the server, so phone and laptop share it
                _ov, _d0 = planning_read.load_overrides(), clock.carmen_day()
                v["plan_notes"] = {k: n for k, n in ((d.isoformat(), planning_read.plan_note(d, _ov))
                                                     for d in (_d0, _d0 + dt.timedelta(days=1))) if n["text"] or n["sent"]}
            except Exception as exc:
                taplog.log_event("error", where="plan_notes", detail=repr(exc)[:200])
        except Exception as exc:
            taplog.log_event("error", where="chat summaries", detail=str(exc)[:200])
        if req.query_params.get("open"):   # a real open, not an auto-refresh
            # What Claude pre-selected, so rows shown but never touched can be found later.
            shown = {side: [r["id"] for r in ([v[side]["one"]] if v[side]["one"] else []) + v[side]["needs"]]
                     for side in ("today", "tomorrow")}
            taplog.log_event("open", src=source(req), counts=v["counts"], shown=shown,
                      replies=[r["id"] for r in v["replies"]], claude=[r["id"] for r in v["claude"]])
        return JSONResponse(web.link_vault_files(v, secret))

    async def act(req: Request):
        if not authed(req):
            return JSONResponse({"error": "unauthorised"}, status_code=403)
        try:
            body = await req.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return JSONResponse({"error": "bad request"}, status_code=400)
        body = web.unlink_vault_files(body)   # an edited Next step comes back with signed links in it
        try:
            # Off the event loop (Nov 1: a slow calendar read inside a tap froze the chat's polling
            # too), and one tap at a time, as before, since taps read, change and write notes.
            return await asyncio.to_thread(_act_locked, req, body)
        except notes.NoteChanged as exc:   # the note changed underneath twice: nothing written
            return JSONResponse({"error": str(exc)}, status_code=409)
        except ValueError as exc:   # apply() refusing: say why, write nothing
            return JSONResponse({"error": str(exc)[:200] or "could not do that"}, status_code=400)
        except Exception as exc:   # a tap never ends in a bare 500; the log has what broke
            taplog.log_event("error", where=f"act {body.get('action')}", detail=repr(exc)[:300])
            return JSONResponse({"error": "something broke, it is logged; refresh and try again"}, status_code=500)

    write_lock = threading.Lock()
    import types
    ctx = types.SimpleNamespace(card_id=card_id, current_view=current_view, find=find, source=source)

    def _act_locked(req: Request, body: dict):
        # A live tap may save the view's bookkeeping too; a test-mode tap writes nothing at all.
        with write_lock, state.persist(not body.get("test")):
            return _act(req, body)

    def tick() -> None:
        """Once a minute in the server (Nov 6): what page loads used to do on the side. Builds the
        view with persisting on, so the day's roll-over, dated tasks, chunk ids, frozen ranks and the
        step history's outside edits are saved; then the talk check, prep, short titles."""
        with write_lock, state.persist():
            try:   # a renamed note keeps its Do place, selection, ticks and chat (renames.py)
                from . import renames
                renames.check(taplog.log_event)
            except Exception as exc:
                taplog.log_event("error", where="tick renames", detail=repr(exc)[:200])
            v = current_view()
            try:
                planning_read.attach_shorts(v)
            except Exception as exc:
                taplog.log_event("error", where="tick shorts", detail=repr(exc)[:200])
        talk.check_talk_deltas()
        runs.maybe_start_prep(v.get("generated", ""))
        from .planning import actions as plan_actions   # Tasks v2: left-over tasks at the day turn
        plan_actions.day_turn()
        task_actions.refine_estimates()   # rules on the write, the model's estimate a minute later

    def _act(req: Request, body: dict):
        from . import actions
        return actions.act(ctx, req, body)


    # --- chat in the app (chat.py, Oct 31): Talk, Let Claude, Save & send, Plan it with Claude
    hub_box: dict = {}

    def hub():
        if "hub" not in hub_box:
            import asyncio
            from .chat import Hub
            hub_box["hub"] = Hub()
            hub_box["hub"].before_queued = lambda key, turn: talk_queued_note(key, turn)
            hub_box["reaper"] = asyncio.create_task(hub_box["hub"].housekeeping())
        return hub_box["hub"]

    def chat_key(kind: str, row_id: str, day_name: str) -> str:
        if kind == "general":   # the Chat tab (Nov 8): one conversation per thread, no card
            from .chat import thread_id
            return "general:" + thread_id(row_id)
        if kind == "plan":
            return "plan:" + (clock.carmen_day() + dt.timedelta(days=1 if day_name == "tomorrow" else 0)).isoformat()
        return "task:" + row_id

    async def day_view(req: Request):
        """The day's calendar for the Plan chat's day view (Oct 31)."""
        if not authed(req):
            return JSONResponse({"error": "unauthorised"}, status_code=403)
        now = dt.datetime.now(clock.TZ)
        d = clock.carmen_day(now) + dt.timedelta(days=1 if req.query_params.get("day") == "tomorrow" else 0)
        ev = calendar.day_events(d, fresh=req.query_params.get("fresh") == "1")
        return JSONResponse({"date": d.isoformat(), "events": ev or [], "ok": ev is not None,
                             "day_start": capacity.DAY_START, "day_end": capacity.DAY_END,
                             "now": now.hour * 60 + now.minute if d == now.date() else None})

    async def chat_get(req: Request):
        if not authed(req):
            return JSONResponse({"error": "unauthorised"}, status_code=403)
        q = req.query_params
        try:
            h = hub()
        except Exception as exc:
            return JSONResponse({"error": f"chat unavailable: {exc}"}, status_code=503)
        rid = q.get("id", "")
        if q.get("kind", "task") not in ("plan", "general") and rid:   # the page may still hold the card's title
            rid = await asyncio.to_thread(card_id, rid, True)
        key = chat_key(q.get("kind", "task"), rid, q.get("day", "today"))
        return JSONResponse(web.link_vault_files(h.state(key), secret))

    FILE_TYPES = {".pdf": "application/pdf", ".md": "text/plain; charset=utf-8",
                  ".txt": "text/plain; charset=utf-8", ".png": "image/png", ".jpg": "image/jpeg",
                  ".jpeg": "image/jpeg", ".csv": "text/plain; charset=utf-8"}

    async def vault_file(req: Request):
        q = req.query_params
        rel = q.get("p", "")
        if not web.verify_file(secret, rel, q.get("e", ""), q.get("s", "")):
            return Response("This link has expired. Open it again from the app.", status_code=403,
                            media_type="text/plain; charset=utf-8")
        rel, _, section = rel.partition("#")
        p = web.resolve_vault_file(rel, config.VAULT)
        if p is None:
            return Response("That file is no longer in the vault.", status_code=404,
                            media_type="text/plain; charset=utf-8")
        from urllib.parse import quote
        taplog.log_event("file_open", path=rel[:200])
        if section and p.suffix.lower() == ".md":
            return HTMLResponse(web.section_page(p, section), headers={"Cache-Control": "private, no-store"})
        return Response(p.read_bytes(),
                        media_type=FILE_TYPES.get(p.suffix.lower(), "application/octet-stream"),
                        headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(p.name)}",
                                 "Cache-Control": "private, no-store"})

    async def chat_post(req: Request):
        if not authed(req):
            return JSONResponse({"error": "unauthorised"}, status_code=403)
        from .chat import Busy
        try:
            body = await req.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return JSONResponse({"error": "bad request"}, status_code=400)
        op, kind, row_id = body.get("op", "send"), body.get("kind", "task"), str(body.get("id", ""))
        day_name = "tomorrow" if body.get("day") == "tomorrow" else "today"
        dry, tab_name, first = bool(body.get("test")), body.get("tab", ""), bool(body.get("first"))
        if kind not in ("plan", "general") and row_id:
            # A chat on a card without a note yet creates it (Nov 4), so the chat keys on the note;
            # Stop and approve find that same chat from the title the page still holds.
            row_id = await asyncio.to_thread(card_id, row_id, dry or op != "send")
            if not dry:
                from .tasks import cardnotes
                await asyncio.to_thread(cardnotes.touch, row_id)
        if op == "note":
            # His note for Claude (r8 F2): one per day, stored with the day's plan. Test mode keeps it
            # on the page only.
            if kind != "plan":
                return JSONResponse({"error": "only a plan has a note"}, status_code=400)
            nday = clock.carmen_day() + dt.timedelta(days=1 if day_name == "tomorrow" else 0)
            if not dry:
                from .planning import actions as plan_actions
                await asyncio.to_thread(plan_actions.save_override, "note", nday, str(body.get("text") or "")[:1000])
            n = planning_read.plan_note(nday)
            return JSONResponse({"ok": True, "note": {**n, "text": str(body.get("text") or "")[:1000]} if dry else n})
        try:
            h = hub()
        except Exception as exc:
            return JSONResponse({"error": f"chat unavailable: {exc}"}, status_code=503)
        key = chat_key(kind, row_id, day_name)
        if op == "approve":
            allow = bool(body.get("allow"))
            taplog.log_event("chat_approval", key=key, allow=allow, test=dry, src=source(req))
            if not h.answer(str(body.get("req", "")), allow):
                return JSONResponse({"error": "that request has expired"}, status_code=410)
            return JSONResponse(web.link_vault_files(h.state(key), secret))
        if op == "stop":
            await h.stop(key)
            return JSONResponse(web.link_vault_files(h.state(key), secret))
        if op == "book":
            # Book (Nov 10, Tier 1.2 and 1.3): the app's tap, never a chat word. It sends exactly the
            # day's open blocks with the [Book] marker; the outcome is read from the calendar.
            if kind != "plan":
                return JSONResponse({"error": "only a plan books"}, status_code=400)
            base_day = clock.carmen_day() + dt.timedelta(days=1 if day_name == "tomorrow" else 0)
            if planning_read.day_over(base_day):
                return JSONResponse({"error": "that day is over: nothing to book"}, status_code=400)
            blocks = (await asyncio.to_thread(planning_read.day_blocks, base_day, None, True))["open"]
            if not blocks:
                return JSONResponse({"error": "nothing to book: every block is in the calendar already"}, status_code=400)
            n = len(blocks)
            shown = f"Book {n} block{'' if n == 1 else 's'} for {base_day.strftime('%A')}"
            taplog.log_event("tap", action="plan_book", id="", test=dry, src=source(req), tab=tab_name, n=n,
                             day=base_day.isoformat())
            try:
                return JSONResponse(web.link_vault_files(h.send(
                    key, f"Plan {clock._label(base_day)}", shown, test=dry, kind="plan",
                    prompt_text=planning_read.book_text(base_day, blocks),
                    book={"day": base_day, "blocks": blocks}), secret))
            except Busy as exc:
                return JSONResponse({"error": str(exc)}, status_code=409)
        return await send_message(req, h, body, key, kind, row_id, day_name, dry, tab_name, first)

    def plan_note_sent(base_day, body, dry) -> str:
        """The note a plan request carries (r8 F2): the page's text when given (saved as the day's
        note), else the stored one; recorded as sent, so Update plan knows if it changed."""
        from .planning import actions as plan_actions
        nt = str(body["note"])[:1000] if isinstance(body.get("note"), str) else planning_read.plan_note(base_day)["text"]
        if not dry:
            plan_actions.save_override("note", base_day, nt)
            plan_actions.save_override("note_sent", base_day, nt)
        return nt.strip()

    async def send_message(req, h, body, key, kind, row_id, day_name, dry, tab_name, first, voice=False):
        from .chat import Busy
        text = str(body.get("text", ""))[:4000].strip()
        images = web.chat_images(body, config.VAULT)
        base_day = clock.carmen_day() + dt.timedelta(days=1 if day_name == "tomorrow" else 0)
        context, row = "", None
        if kind == "plan":
            title = f"Plan {clock._label(base_day)}"
            if not text and not images:
                ids, over = planning_read.plan_ids(body)
                # Each planned card is a note first, so the plan names its ID and the block it
                # gets carries a Task: line (Nov 4: Eli's call sat unslotted under a batch block).
                ids = [await asyncio.to_thread(card_id, i, dry) for i in ids]
                if not dry:
                    from .tasks import cardnotes
                    for i in ids:
                        await asyncio.to_thread(cardnotes.touch, i)
                v = await asyncio.to_thread(current_view)
                picked, seen = [], set()
                for i in ids:
                    r = next((r for r in planning_read.all_rows(v) if r["id"] == i), None)
                    if r and r["id"] not in seen:
                        seen.add(r["id"]); picked.append(r)
                if not picked:
                    return JSONResponse({"error": "nothing selected to plan"}, status_code=400)
                # The note always goes with the first plan request (r8 F2): the page's current
                # text when it sends one, else the day's stored note.
                nt = await asyncio.to_thread(plan_note_sent, base_day, body, dry)
                text = planning_read.plan_text(base_day, picked, ending=False, note=nt) + planning_read.plan_gaps_note(
                    over, [i for i in dict.fromkeys(ids) if i not in seen], "plan ids (chat)")
            elif body.get("update"):
                # Update plan (r8 F2): his note goes again only when it changed since the plan chat got it.
                was = planning_read.plan_note(base_day)["sent"].strip()
                nt = await asyncio.to_thread(plan_note_sent, base_day, body, dry)
                text = text + (f" My note now: {nt}" if nt and nt != was else " Leave out my earlier note." if was and not nt else "") \
                    + " Please revise the proposal."
            if first:
                taplog.log_event("tap", action="plan", id="", test=dry, src=source(req), tab=tab_name, edited=False)
        elif kind == "general":
            if not text and not images:
                return JSONResponse({"error": "empty message"}, status_code=400)
            from .chat import thread_title
            title = load_title(key) or thread_title(text, bool(images))
            taplog.log_event("tap", action="chat", id=key, test=dry, src=source(req), tab=tab_name, edited=True)
        else:
            row = await asyncio.to_thread(find, row_id)
            title = (row or {}).get("title") or load_title(key) or row_id
            if not text and not images:
                return JSONResponse({"error": "empty message"}, status_code=400)
            if first and row:
                # The same signals the Telegram Talk logged (claude_go, reply_talk, talk), at the
                # moment the first message goes, as the old tap sent at once.
                taplog.log_event("tap", action="talk", id=row_id, test=dry, src=source(req), tab=tab_name, edited=True)
                taplog.signal({"claude": "claude_go", "replies": "reply_talk"}.get(tab_name, "talk"), row, day_name,
                       test=dry, note=notes._one_line(text), via="app chat", opened_by=str(body.get("from", ""))[:20])
                if not dry:
                    talk.remember_talk(row)
                context = talk.talk_text(row, ending=False)
            elif not row and not load_title(key):
                return JSONResponse({"error": "row not found, refresh"}, status_code=404)
            else:
                taplog.log_event("tap", action="chat", id=row_id, test=dry, src=source(req), tab=tab_name, edited=True)
        # Every message he writes is judgment data (Oct 31, the owner: chat should feed the signals too):
        # his words verbatim, next to what Claude had suggested for the row (signal's `claude`).
        taplog.signal("chat_message", row or {"id": row_id or key, "title": title}, day_name, test=dry, key=key,
               text=text[:2000], voice=voice, first=first, src=source(req), images=len(images),
               **({"ids": [str(i) for i in (body.get("ids") or [])]} if kind == "plan" else {}))
        try:
            if voice:
                from jobs.tg_bridge.media import frame_transcript
                from . import speech
                return JSONResponse(web.link_vault_files(h.send(key, title, text, context=context, test=dry, kind=kind,
                                           prompt_text=frame_transcript(text) + (speech.TALK_NOTE if body.get("talk") else ""),
                                           voice=True), secret))
            if images:
                from urllib.parse import quote
                said = "\n".join(f"[the owner attached a PDF, saved at {rel} (absolute: {config.VAULT / rel}). "
                                 "Read it to see it; past 10 pages Read needs a pages range.]"
                                 if rel.lower().endswith(".pdf") else
                                 f"[the owner attached a screenshot, saved at {rel} (absolute: "
                                 f"{config.VAULT / rel}). Read it to see it.]" for rel in images)
                links = ["obsidian://open?vault=<VAULT_NAME>&file=" + quote(rel, safe="") for rel in images]
                return JSONResponse(web.link_vault_files(h.send(key, title, text, context=context, test=dry, kind=kind,
                                           prompt_text=f"{text}\n\n{said}".strip(), images=links), secret))
            # Start on Do (Nov 1): the request to begin goes to Claude but not on his screen.
            return JSONResponse(web.link_vault_files(h.send(key, title, text, context=context, test=dry, kind=kind,
                                       hidden=bool(body.get("hidden"))), secret))
        except Busy as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    async def chat_image(req: Request):
        """A screenshot or photo for the chat (Nov 2, the owner: "I want to be able to attach
        screenshots when texting with carmen"). Saved into Attachments/Carmen/<YYYY-MM>/ with the
        bridge's naming; the message that carries it hands Claude the path, as Telegram does."""
        if not authed(req):
            return JSONResponse({"error": "unauthorised"}, status_code=403)
        ext = web.ATTACH_TYPES.get(req.headers.get("content-type", "").split(";")[0].strip().lower())
        if not ext:
            return JSONResponse({"error": "only images and PDFs can be attached"}, status_code=415)
        data = await req.body()
        if not data:
            return JSONResponse({"error": "empty image"}, status_code=400)
        if len(data) > web.IMAGE_MAX_BYTES:
            return JSONResponse({"error": "image too large"}, status_code=413)
        now = dt.datetime.now(clock.TZ)
        folder = config.VAULT / web.IMAGE_DIR / now.strftime("%Y-%m")
        folder.mkdir(parents=True, exist_ok=True)
        base_name = f"{now.strftime('%Y-%m-%d %H%M%S')} screenshot"
        if ext == "pdf":
            if not data.startswith(b"%PDF"):
                return JSONResponse({"error": "not a PDF"}, status_code=415)
            base_name = f"{now.strftime('%Y-%m-%d %H%M%S')} {web.attach_stem(req.query_params.get('name', ''))}"
        path, n = folder / f"{base_name}.{ext}", 2
        while path.exists():
            path, n = folder / f"{base_name} {n}.{ext}", n + 1
        with open(path, "xb") as fh:
            fh.write(data)
        rel = path.relative_to(config.VAULT).as_posix()
        taplog.log_event("chat_image", bytes=len(data), src=source(req))
        return JSONResponse({"path": rel, "url": web.sign_file(secret, rel)})

    async def chat_voice(req: Request):
        """A voice message (Oct 31): the recording goes up as the raw body, is transcribed by the
        bridge's own Transcriber (Groq, then OpenAI), never kept, and continues as a message."""
        if not authed(req):
            return JSONResponse({"error": "unauthorised"}, status_code=403)
        from jobs.tg_bridge import media
        q = req.query_params
        audio = await req.body()
        if len(audio) > web.VOICE_MAX_BYTES:
            return JSONResponse({"error": "recording too long"}, status_code=413)
        try:
            h = hub()
        except Exception as exc:
            return JSONResponse({"error": f"chat unavailable: {exc}"}, status_code=503)
        ctype = req.headers.get("content-type", "")
        ext = "m4a" if "mp4" in ctype or "m4a" in ctype else "webm" if "webm" in ctype else "ogg"
        try:
            text, provider = await media.Transcriber(media.providers_from_env()).transcribe(audio, f"voice.{ext}")
        except media.MediaError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        finally:
            del audio
        taplog.log_event("chat_voice", provider=provider, chars=len(text), src=source(req))
        if not text:
            return JSONResponse({"error": "no speech could be made out"}, status_code=422)
        body = {"text": text, "ids": q.getlist("ids"), "from": q.get("from", ""), "talk": q.get("talk") == "1"}
        day_name = "tomorrow" if q.get("day") == "tomorrow" else "today"
        kind, row_id = q.get("kind", "task"), q.get("id", "")
        if kind not in ("plan", "general") and row_id:
            row_id = await asyncio.to_thread(card_id, row_id, q.get("test") == "1")
            if q.get("test") != "1":
                from .tasks import cardnotes
                await asyncio.to_thread(cardnotes.touch, row_id)
        return await send_message(req, h, body, chat_key(kind, row_id, day_name), kind, row_id, day_name,
                            q.get("test") == "1", q.get("tab", ""), q.get("first") == "1", voice=True)

    async def chat_tts(req: Request):
        """Claude's reply read aloud (Nov 8, speech.py): the chat's own stored text at the indexes
        `i`, never text the page sends, as mp3 from the configured provider, cached by hash."""
        if not authed(req):
            return JSONResponse({"error": "unauthorised"}, status_code=403)
        from . import speech
        q = req.query_params
        try:
            h = hub()
        except Exception as exc:
            return JSONResponse({"error": f"chat unavailable: {exc}"}, status_code=503)
        rid = q.get("id", "")
        if q.get("kind", "task") not in ("plan", "general") and rid:
            rid = await asyncio.to_thread(card_id, rid, True)
        try:
            idx = [int(x) for x in q.getlist("i")][:12]
        except ValueError:
            return JSONResponse({"error": "bad message index"}, status_code=400)
        st = h.state(chat_key(q.get("kind", "task"), rid, q.get("day", "today")))
        text = speech.speakable(speech.reply_text(st, idx))
        if not text:
            return JSONResponse({"error": "nothing to read aloud"}, status_code=404)
        try:
            audio, provider, cached = await speech.synthesize(text, speech.providers_from_env(env))
        except speech.SpeechError as exc:
            taplog.log_event("error", where="chat_tts", detail=str(exc)[:200])
            return JSONResponse({"error": str(exc)}, status_code=502)
        taplog.log_event("chat_tts", provider=provider, chars=len(text), cached=cached, src=source(req))
        return Response(audio, media_type="audio/mpeg", headers={"cache-control": "private, max-age=86400"})

    async def chat_pending(req: Request):
        """Approvals waiting in any chat (Nov 8): the banner on every tab, Allow and Deny."""
        if not authed(req):
            return JSONResponse({"error": "unauthorised"}, status_code=403)
        try:
            h = hub()
        except Exception:
            return JSONResponse({"pending": []})
        if req.method == "POST":
            try:
                body = await req.json()
            except ValueError:
                return JSONResponse({"error": "bad request"}, status_code=400)
            allow = bool(body.get("allow"))
            taplog.log_event("chat_approval", key=str(body.get("key", ""))[:80], allow=allow,
                             test=False, src=source(req) + ":banner")
            if not h.answer(str(body.get("req", "")), allow):
                return JSONResponse({"error": "that request has expired"}, status_code=410)
        return JSONResponse({"pending": h.pending()})

    async def chat_threads(req: Request):
        """The Chat tab's past threads (Nov 8), newest first."""
        if not authed(req):
            return JSONResponse({"error": "unauthorised"}, status_code=403)
        from .chat import threads
        return JSONResponse({"threads": await asyncio.to_thread(threads)})

    talk_seen: dict[str, int] = {}   # Siri thread -> first message index not yet spoken (memory only)
    # Talk's delivery ledger (Nov 11, memory only): a turn's answer is spoken exactly once.
    talk_late: dict[str, dict[int, float]] = {}   # key -> turns that outlived their stream -> when
    talk_spoken: dict[str, list[int]] = {}        # key -> turns whose answer was spoken (last 50)
    talk_open: dict[str, int] = {}                # key -> streams open right now
    talk_late_told: dict[str, bool] = {}          # key -> a late answer went out by the poll: tell Claude

    def mark_spoken(key: str, turn) -> None:
        if turn is not None:
            talk_spoken[key] = (talk_spoken.get(key, []) + [turn])[-50:]

    def talk_queued_note(key: str, finished_turn) -> str:
        """chat.Hub.before_queued: a Talk turn queued behind one that outlived its stream."""
        return siri_mod().LATE_QUEUED_NOTE if finished_turn in talk_late.get(key, {}) else ""

    def siri_mod():
        from . import siri
        return siri

    async def talk_siri(req: Request):
        """Carmen through Siri (Nov 8, siri.py): the "Carmen" Shortcut POSTs {thread, text} with
        x-carmen-token; the words go to that Chat thread as a voice message, the reply is waited
        for (at most siri.WAIT_S) and comes back as {"reply": <spoken text>}. Empty text: what is
        new since the last answer."""
        from . import siri
        if not siri.authorised(req.headers.get("x-carmen-token", ""), env.get("CARMEN_TALK_TOKEN", "")):
            return JSONResponse({"error": "unauthorised"}, status_code=403)
        from .chat import Busy, thread_id, thread_title
        try:
            body = await req.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return JSONResponse({"error": "bad request"}, status_code=400)
        try:
            h = hub()
        except Exception as exc:
            return JSONResponse({"reply": "Carmen is not reachable right now.", "error": str(exc)}, status_code=503)
        key = "general:" + thread_id(body.get("thread") or "siri")
        text, dry = str(body.get("text", ""))[:2000].strip(), not siri.live(env)
        if text:
            from jobs.tg_bridge.media import frame_transcript
            talk_seen.setdefault(key, len(h.state(key)["messages"]))
            taplog.log_event("chat_siri", key=key, chars=len(text), test=dry)
            from . import talkcall   # the idle fallback recaps it (Nov 11)
            talkcall.touch(key.split(":", 1)[1], key.split(":", 1)[1], src="siri", test=dry)
            try:
                h.send(key, load_title(key) or thread_title("Siri: " + text), text, test=dry, kind="general",
                       prompt_text=frame_transcript(text) + siri.SIRI_NOTE, voice=True)
            except Busy as exc:
                return JSONResponse({"reply": "Carmen is busy, try again in a moment.", "error": str(exc)}, status_code=409)
        st = await siri.wait_idle(h.state, key)
        said, talk_seen[key] = siri.spoken_reply(st, talk_seen.get(key, len(st["messages"])))
        return JSONResponse({"reply": said})

    async def talk_voice(req: Request):
        """The iPhone app's background Talk (Nov 8, siri.py § APP_NOTE): the app records natively and
        POSTs the recording as the raw body with ?thread=<id>; it is transcribed like a voice message,
        sent to that Chat thread, the reply waited for (siri.APP_WAIT_S) and returned as
        {heard, reply, busy, audio: base64 mp3 or "", end: true when Claude said goodbye and the app should
        hang up after playing it}. An empty body asks what is new."""
        if not authed(req):
            return JSONResponse({"error": "unauthorised"}, status_code=403)
        from . import siri, speech
        from .chat import Busy, thread_id, thread_title
        from jobs.tg_bridge import media
        q = req.query_params
        audio = await req.body()
        if len(audio) > web.VOICE_MAX_BYTES:
            return JSONResponse({"error": "recording too long"}, status_code=413)
        try:
            h = hub()
        except Exception as exc:
            return JSONResponse({"reply": "Carmen is not reachable right now.", "error": str(exc)}, status_code=503)
        from . import talkcall
        name = thread_id(q.get("thread") or "talk")
        key = "general:" + talkcall.resolve(name)[0]   # a new Talk within 2 h continues the last (Nov 11)
        dry, heard, my_turn, polling = q.get("test") == "1", "", None, not audio
        if audio:
            ctype = req.headers.get("content-type", "")
            ext = "m4a" if "mp4" in ctype or "m4a" in ctype or "aac" in ctype else "wav" if "wav" in ctype else "ogg"
            try:
                heard, provider, noise_why = await noise_gate(h, key, audio, ext)
            except media.MediaError as exc:
                return JSONResponse({"error": str(exc), "reply": "I couldn't hear that, say it again."}, status_code=502)
            finally:
                del audio
            heard = (heard or "").strip()[:2000]
            dropped = bool(noise_why) or siri.unheard(heard)
            taplog.log_event("chat_talk_app", key=key, provider=provider, chars=len(heard), test=dry, dropped=dropped,
                             noise=noise_why or None)
            if dropped:   # the app says "Sorry, I didn't catch that" from its own clip
                return JSONResponse({"heard": "", "reply": "", "busy": False, "audio": ""})
            if heard:
                from jobs.tg_bridge.media import frame_transcript
                talk_seen.setdefault(key, len(h.state(key)["messages"]))
                talkcall.touch(name, key.split(":", 1)[1], src="app", test=dry)
                told = siri.LATE_TOLD_NOTE if talk_late_told.pop(key, False) else ""
                try:
                    sent_st = h.send(key, load_title(key) or thread_title("Talk: " + heard), heard, test=dry, kind="general",
                                     prompt_text=frame_transcript(heard) + siri.APP_NOTE + talkcall.resume_note(name)
                                     + told, voice=True)
                    my_turn = (sent_st or {}).get("sent_turn")
                except Busy as exc:
                    return JSONResponse({"heard": heard, "reply": "Carmen is busy, try again in a moment.",
                                         "busy": True, "audio": "", "error": str(exc)}, status_code=409)
        if polling:
            # A poll for a handed-off turn (Nov 11): only turns that outlived their stream, each once,
            # and nothing while a stream is open on this Talk (that stream says it, framed).
            late = talk_late.get(key) or {}
            if talk_open.get(key):
                return JSONResponse({"heard": "", "reply": "", "busy": False, "audio": "", "end": False})
            if late:
                end_at = asyncio.get_running_loop().time() + siri.APP_WAIT_S
                ready = []
                while True:
                    msgs = h.state(key).get("messages") or []
                    ready = [(t, siri.turn_answer(msgs, t)[0]) for t in sorted(late) if siri.turn_answer(msgs, t)[1]]
                    if ready or talk_open.get(key) or asyncio.get_running_loop().time() > end_at:
                        break
                    await asyncio.sleep(siri.POLL_S)
                if talk_open.get(key):
                    return JSONResponse({"heard": "", "reply": "", "busy": False, "audio": "", "end": False})
                parts = []
                for t, txt in ready:
                    late.pop(t, None)
                    mark_spoken(key, t)
                    if txt.strip():
                        parts.append(txt)
                if ready:
                    talk_late_told[key] = True
                msgs = h.state(key).get("messages") or []
                later = any(m.get("role") == "you" and (m.get("turn") or 0) > max(t for t, _ in ready)
                            for m in msgs) if ready else False
                said = speech.speakable("\n\n".join(parts), limit=siri.SPOKEN_MAX)
                said = siri.late_lead(said, siri.looks_german(said)) if later and said else said
                said, ending = siri.split_end(said)
                talk_seen[key] = len(msgs)
                st = {"busy": bool(late)}
            else:
                st = await siri.wait_idle(h.state, key, timeout=siri.APP_WAIT_S)
                done_turns = set(talk_spoken.get(key, []))   # never again what a stream already said
                st = {**st, "messages": [({**m, "hidden": True} if m.get("turn") in done_turns else m)
                                         for m in st.get("messages") or []]}
                said, talk_seen[key] = siri.spoken_reply(st, talk_seen.get(key, len(st["messages"])))
                said, ending = siri.split_end(said)
        else:
            st = await siri.wait_idle(h.state, key, timeout=siri.APP_WAIT_S)
            said, talk_seen[key] = siri.spoken_reply(st, talk_seen.get(key, len(st["messages"])))
            said, ending = siri.split_end(said)
            if heard and my_turn is not None:
                if siri.turn_answer(st.get("messages") or [], my_turn)[1]:
                    mark_spoken(key, my_turn)
                elif not st.get("approval"):
                    talk_late.setdefault(key, {})[my_turn] = asyncio.get_running_loop().time()
        clip = ""
        try:
            mp3, _, _ = await speech.synthesize(said, speech.providers_from_env(env))
            import base64
            clip = base64.b64encode(mp3).decode()
        except Exception as exc:  # the app falls back to the phone's own voice
            taplog.log_event("error", where="talk_voice_tts", detail=str(exc)[:200])
        if ending and not st.get("busy"):   # goodbye by voice: the recap goes out now
            talkcall.end(name)
            kick_recaps()
        return JSONResponse({"heard": heard, "reply": said, "busy": bool(st.get("busy")), "audio": clip,
                             "end": ending})

    talk_fragment: dict[str, tuple[str, float]] = {}   # thread -> (half-said words, when), memory only

    async def noise_gate(h, key: str, audio: bytes, ext: str) -> tuple[str, str, str]:
        """(heard, provider, why it is noise or ""): Talk's transcription with noise.py's two checks
        (Nov 9, street noise became turns). Looser right after a question or while an approval
        waits, so a short "ja" still counts."""
        from . import noise
        from jobs.tg_bridge import media
        heard, provider, segments = await media.Transcriber(media.providers_from_env()).transcribe_detail(
            audio, f"voice.{ext}")
        if not (heard or "").strip():
            return "", provider, ""
        try:
            st = h.state(key)
            loose = noise.asked(st) or any(a["key"] == key for a in h.pending())
        except Exception:
            loose = True
        speech_ms = await asyncio.to_thread(noise.speech_ms, audio) if ext == "wav" else None
        return heard, provider, noise.drop_reason(segments, speech_ms, loose)

    async def talk_stream(req: Request):
        """The iPhone app's streamed Talk (Nov 8): like /api/talk/voice, but the answer streams as
        NDJSON lines while Claude works: {"t": "heard", text}, {"t": "say", text, audio} for each text
        block the moment it exists (a short "what I'll do" first, then the result), {"t": "step", text}
        when the tool step changes, {"t": "more"} when the words end mid-thought (held and joined
        with the next turn), and last {"t": "done", busy, end}. busy true: the turn runs on; the app
        asks /api/talk/voice with an empty body later, which says what finished. done comes only
        after the turn's final answer was sent (minus what its text blocks already said,
        siri.unsaid), or with busy true (Nov 11). The thread goes through talkcall.resolve."""
        if not authed(req):
            return JSONResponse({"error": "unauthorised"}, status_code=403)
        from . import siri, speech
        from .chat import Busy, thread_id, thread_title
        from jobs.tg_bridge import media
        import base64
        import json as _json
        import time as _time
        from starlette.responses import StreamingResponse
        q = req.query_params
        audio = await req.body()
        if len(audio) > web.VOICE_MAX_BYTES:
            return JSONResponse({"error": "recording too long"}, status_code=413)
        try:
            h = hub()
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)
        from . import talkcall
        name = thread_id(q.get("thread") or "talk")
        key = "general:" + talkcall.resolve(name)[0]   # a new Talk within 2 h continues the last (Nov 11)
        dry = q.get("test") == "1"
        ctype = req.headers.get("content-type", "")
        ext = "m4a" if "mp4" in ctype or "m4a" in ctype or "aac" in ctype else "wav" if "wav" in ctype else "ogg"
        providers = speech.providers_from_env(env)

        async def voice(text: str) -> str:
            try:
                mp3, _, _ = await speech.synthesize(text, providers)
                return base64.b64encode(mp3).decode()
            except Exception as exc:
                taplog.log_event("error", where="talk_stream_tts", detail=str(exc)[:200])
                return ""

        def line(obj: dict) -> bytes:
            return (_json.dumps(obj, ensure_ascii=False) + "\n").encode()

        async def events():
            t0 = _time.time()
            try:
                heard, provider, noise_why = await noise_gate(h, key, audio, ext)
            except media.MediaError as exc:
                yield line({"t": "heard", "text": "", "error": str(exc)})
                yield line({"t": "done", "busy": False, "end": False})
                return
            heard = (heard or "").strip()[:2000]
            t_heard = _time.time() - t0
            if noise_why or siri.unheard(heard):
                taplog.log_event("chat_talk_stream", key=key, dropped=True, t_heard=round(t_heard, 2),
                                 noise=noise_why or None, chars=len(heard))
                yield line({"t": "heard", "text": ""})
                yield line({"t": "done", "busy": False, "end": False})
                return
            frag = talk_fragment.pop(key, None)
            if frag and _time.time() - frag[1] < 25:
                heard = (frag[0] + " " + heard).strip()
            if siri.unfinished(heard) and q.get("final") != "1" and not any(a["key"] == key for a in h.pending()):
                talk_fragment[key] = (heard, _time.time())
                taplog.log_event("chat_talk_stream", key=key, held=True, chars=len(heard), t_heard=round(t_heard, 2))
                yield line({"t": "more", "text": heard})
                return
            talkcall.touch(name, key.split(":", 1)[1], src="app", test=dry)
            yield line({"t": "heard", "text": heard})
            from jobs.tg_bridge.media import frame_transcript
            st0 = h.state(key)
            german = siri.looks_german(heard)
            waiting = next((a for a in h.pending() if a["key"] == key), None)
            resumed = False
            if waiting:
                # An approval waits in this Talk: his words are the answer, not a new message.
                verdict = siri.voice_verdict(heard)
                if verdict is None:
                    ask = siri.approval_line(waiting, german)
                    yield line({"t": "say", "text": ask, "audio": await voice(ask)})
                    yield line({"t": "done", "busy": True, "end": False, "approval": True})
                    return
                taplog.log_event("chat_approval", key=key, allow=verdict, test=dry, src="talk:voice")
                h.answer(waiting["id"], verdict)
                ok = ("Okay, erlaubt." if verdict else "Okay, lass ich.") if german else ("Okay, allowed." if verdict else "Okay, I won't.")
                yield line({"t": "say", "text": ok, "audio": await voice(ok)})
                resumed = True
            seen = talk_seen.get(key, len(st0["messages"])) if resumed else len(st0["messages"])
            talk_seen[key] = seen   # a later poll says only what this turn adds
            my_turn = st0.get("turn") if resumed else None   # the turn this stream speaks for (Nov 11)
            try:
                if resumed:
                    raise StopIteration   # the turn runs on after the approval; nothing new to send
                told = siri.LATE_TOLD_NOTE if talk_late_told.pop(key, False) else ""
                sent_st = h.send(key, load_title(key) or thread_title("Talk: " + heard), heard, test=dry, kind="general",
                                 prompt_text=frame_transcript(heard) + siri.APP_NOTE + siri.STREAM_NOTE
                                 + talkcall.resume_note(name) + told, voice=True)
                my_turn = (sent_st or {}).get("sent_turn")
            except StopIteration:
                pass
            except Busy as exc:
                yield line({"t": "say", "text": "Carmen is busy, try again in a moment.", "audio": ""})
                yield line({"t": "done", "busy": True, "end": False})
                return
            spoken, said_n, step, ending, t_first, asked, final_said, leads = [], 0, "", False, None, False, False, 0
            said_turn = h.state(key).get("turn")
            if resumed:
                said_n = len(h.state(key).get("said") or [])   # what was said before the approval was heard
            end_at = _time.time() + siri.APP_WAIT_S
            talk_open[key] = talk_open.get(key, 0) + 1
            try:
                while True:
                    st = h.state(key)
                    msgs = st.get("messages") or []
                    # A turn that outlived its own stream: its answer once, framed, before this one's.
                    for t in sorted(talk_late.get(key, {})):
                        if t == my_turn:
                            continue
                        txt, over = siri.turn_answer(msgs, t)
                        if not over:
                            continue
                        talk_late[key].pop(t, None)
                        mark_spoken(key, t)
                        lead = siri.late_lead(speech.speakable(txt, limit=siri.SPOKEN_MAX), german)
                        if lead:
                            leads += 1
                            t_first = t_first or _time.time() - t0
                            yield line({"t": "say", "text": lead, "audio": await voice(lead)})
                    said = st.get("said") or []
                    if st.get("turn") != said_turn:
                        said_n, said_turn = 0, st.get("turn")   # another turn owns said now
                    elif len(said) < said_n:
                        said_n = 0   # a queued turn started after the last one (said resets): its blocks are new
                    if my_turn is None or st.get("turn") == my_turn:   # only this stream's own turn speaks
                        for raw in said[said_n:]:
                            text, e = siri.split_end(speech.speakable(raw, limit=siri.SPOKEN_MAX))
                            ending = ending or e
                            if text and text not in spoken:
                                spoken.append(text)
                                t_first = t_first or _time.time() - t0
                                yield line({"t": "say", "text": text, "audio": await voice(text)})
                    said_n = len(said)
                    if st.get("status") and st["status"] != step:
                        step = st["status"]
                        yield line({"t": "step", "text": step})
                    if st.get("approval"):
                        ap = next((a for a in h.pending() if a["key"] == key), None) or dict(st["approval"] or {})
                        ask = siri.approval_line(ap, german)
                        yield line({"t": "say", "text": ask, "audio": await voice(ask)})
                        asked = True
                        break
                    if my_turn is not None:
                        txt, over = siri.turn_answer(msgs, my_turn)
                        final = ""
                        if over:
                            final, e = siri.split_end(speech.speakable(txt, limit=siri.SPOKEN_MAX))
                            ending = ending or e
                    elif not st.get("busy"):
                        # A hub without turn ids: the saved final answer since this turn began.
                        over = True
                        new = [i for i in range(seen, len(msgs)) if msgs[i].get("role") == "claude"
                               and not msgs[i].get("hidden") and not msgs[i].get("unprompted")]
                        final, e = siri.split_end(speech.speakable(speech.reply_text(st, new), limit=siri.SPOKEN_MAX))
                        if not final:   # the turn failed or was stopped: its note is the answer
                            notes_ = [m["text"] for m in msgs[seen:] if m.get("role") == "note" and not m.get("recap")]
                            final = speech.speakable(notes_[-1], limit=siri.SPOKEN_MAX) if notes_ else ""
                        ending = ending or e
                    else:
                        over = False
                    if over:
                        # The final answer, minus what its text blocks already said (siri.unsaid).
                        rest = siri.unsaid(final, spoken)
                        if rest:
                            spoken.append(rest)
                            final_said = True
                            t_first = t_first or _time.time() - t0
                            yield line({"t": "say", "text": rest, "audio": await voice(rest)})
                        if my_turn is not None:
                            mark_spoken(key, my_turn)
                            talk_late.get(key, {}).pop(my_turn, None)
                        talk_seen[key] = len(msgs)
                        break
                    if _time.time() > end_at:
                        break
                    await asyncio.sleep(0.3)
            finally:
                talk_open[key] = max(0, talk_open.get(key, 1) - 1)
            st_end = h.state(key)
            if my_turn is not None:
                busy = not siri.turn_answer(st_end.get("messages") or [], my_turn)[1]
                if busy and not asked:   # it outlives this stream: spoken once later (poll or next stream)
                    talk_late.setdefault(key, {})[my_turn] = _time.time()
            else:
                busy = bool(st_end.get("busy"))
            taplog.log_event("chat_talk_stream", key=key, provider=provider, chars=len(heard), test=dry,
                             t_heard=round(t_heard, 2), t_first=round(t_first, 2) if t_first else None,
                             t_total=round(_time.time() - t0, 2), says=len(spoken), final=final_said, busy=busy,
                             turn=my_turn, leads=leads or None,
                             thread=key.split(":", 1)[1] if name != key.split(":", 1)[1] else None)
            yield line({"t": "done", "busy": busy, "end": ending and not busy, "approval": asked})
            if ending and not busy:   # goodbye by voice: the recap goes out now
                talkcall.end(name)
                kick_recaps()

        return StreamingResponse(events(), media_type="application/x-ndjson",
                                 headers={"cache-control": "no-store", "x-accel-buffering": "no"})

    recap_tasks: set = set()

    async def talk_recaps() -> int:
        """Every Talk session whose recap is due (talkcall.due): claimed once, then delivered."""
        from . import talkcall
        h = hub_box.get("hub")
        busy = (lambda t: bool(h.state("general:" + t).get("busy"))) if h else (lambda t: False)
        n = 0
        for name, s in await asyncio.to_thread(talkcall.due, busy, talkcall.last_message_at):
            if not talkcall.claim(name, s.get("seg", 0)):
                continue
            try:
                await talkcall.deliver(name, s, h.pending() if h else [])
                n += 1
            except Exception as exc:
                taplog.log_event("error", where="talk_recap", detail=repr(exc)[:200])
        return n

    def kick_recaps() -> None:
        t = asyncio.get_running_loop().create_task(talk_recaps())
        recap_tasks.add(t)
        t.add_done_callback(recap_tasks.discard)

    async def talk_end(req: Request):
        """The app's end of a Talk (Nov 11): POST /api/talk/end?thread=<the Talk's name>, empty body
        (or {"thread"} as JSON), with the login cookie or x-carmen-token. Tolerant: an unknown,
        already ended or recapped Talk answers 200 too. The recap goes out once the turn is over."""
        from . import siri, talkcall
        from .chat import thread_id
        if not (authed(req) or siri.authorised(req.headers.get("x-carmen-token", ""), env.get("CARMEN_TALK_TOKEN", ""))):
            return JSONResponse({"error": "unauthorised"}, status_code=403)
        raw = req.query_params.get("thread") or ""
        if not raw:
            try:
                body = await req.json()
                raw = str(body.get("thread") or "") if isinstance(body, dict) else ""
            except ValueError:
                raw = ""
        if not raw:
            return JSONResponse({"ok": False, "error": "no thread"})
        name = thread_id(raw)
        s = talkcall.end(name)
        state_ = "none" if not s else "done" if s.get("recapped") else "queued"
        taplog.log_event("talk_end", name=name, thread=talkcall.target(name), session=state_)
        if state_ == "queued":
            kick_recaps()
        return JSONResponse({"ok": True, "thread": talkcall.target(name), "recap": state_})

    def load_title(key: str) -> str:
        from .chat import load
        return load(key).get("title", "")

    async def shutdown():
        if "hub" in hub_box:
            hub_box["reaper"].cancel()
            await hub_box["hub"].close()

    async def journal(req: Request):
        if not authed(req):
            return JSONResponse({"error": "unauthorised"}, status_code=403)
        try:
            return JSONResponse(await asyncio.to_thread(meditation_entry.journal_entry, clock.carmen_day()))
        except OSError as exc:   # the template moved or the entry is unreadable: say which, never a 500
            taplog.log_event("error", where="journal", detail=repr(exc)[:200])
            return JSONResponse({"error": f"the meditation could not be read ({getattr(exc, 'filename', '') or exc})"},
                                status_code=503)

    async def open_tasks(req: Request):
        """All open tasks, with search (Nov 9, QA item 13): GET ?q=words."""
        if not authed(req):
            return JSONResponse({"error": "unauthorised"}, status_code=403)
        from .tasks import read as tasks_read
        q = (req.query_params.get("q") or "")[:200]
        rows = await asyncio.to_thread(tasks_read.all_open, q)
        return JSONResponse({"q": q, "rows": rows, "n": len(rows)})

    from .direction.goals import routes as goals_routes   # the Goals tab (Nov 5), jobs/checkin_app/goals.py
    routes = []
    for base in ("", "/checkin"):   # with or without the Funnel stripping its path
        routes += [Route(f"{base}/", page), Route(f"{base}/api/view", view),
                   Route(f"{base}/api/journal", journal), Route(f"{base}/file", vault_file),
                   Route(f"{base}/api/open", open_tasks),
                   Route(f"{base}/login", login, methods=["GET", "POST"]),
                   *[Route(f"{base}/{name}", static) for name in STATIC],
                   Route(f"{base}/api/act", act, methods=["POST"]),
                   Route(f"{base}/api/chat", chat_get), Route(f"{base}/api/chat", chat_post, methods=["POST"]),
                   Route(f"{base}/api/chat/voice", chat_voice, methods=["POST"]),
                   Route(f"{base}/api/chat/tts", chat_tts),
                   Route(f"{base}/api/chat/threads", chat_threads),
                   Route(f"{base}/api/chat/pending", chat_pending, methods=["GET", "POST"]),
                   Route(f"{base}/api/talk", talk_siri, methods=["POST"]),
                   Route(f"{base}/api/talk/voice", talk_voice, methods=["POST"]),
                   Route(f"{base}/api/talk/stream", talk_stream, methods=["POST"]),
                   Route(f"{base}/api/talk/end", talk_end, methods=["POST"]),
                   Route(f"{base}/api/chat/image", chat_image, methods=["POST"]),
                   Route(f"{base}/api/day", day_view),
                   *goals_routes(base, authed)]
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):   # Starlette 1.x: no on_shutdown; warm Claude clients close here
        import asyncio

        async def outcomes_loop():   # the server runs all day, so it keeps the 03:55 snapshot itself (end of the Carmen day)
            while True:
                try:
                    await asyncio.to_thread(outcomes.record_outcomes)
                except Exception as exc:
                    taplog.log_event("error", where="day_outcome", detail=str(exc)[:200])
                await asyncio.sleep(300)
        async def watch_loop():   # health every 15 min, pushed when red (alerts.py, Nov 6)
            from . import alerts
            await asyncio.sleep(60)
            while True:
                try:
                    await asyncio.to_thread(alerts.watch)
                except Exception as exc:
                    taplog.log_event("error", where="health watch", detail=repr(exc)[:200])
                await asyncio.sleep(900)
        async def tick_loop():
            while True:
                try:
                    await asyncio.to_thread(tick)
                except Exception as exc:
                    taplog.log_event("error", where="tick", detail=repr(exc)[:200])
                await asyncio.sleep(60)
        async def recap_loop():   # a Talk with no end call: recapped IDLE_S after its last turn (Nov 11)
            while True:
                await asyncio.sleep(60)
                try:
                    await talk_recaps()
                except Exception as exc:
                    taplog.log_event("error", where="talk recap loop", detail=repr(exc)[:200])
        task = asyncio.create_task(outcomes_loop())
        watcher = asyncio.create_task(watch_loop())
        ticker = asyncio.create_task(tick_loop())
        recapper = asyncio.create_task(recap_loop())
        yield
        task.cancel()
        watcher.cancel()
        ticker.cancel()
        recapper.cancel()
        await shutdown()
    async def health(req: Request):
        # Localhost only (the Funnel proxies from 127.0.0.1 too, so it also needs the login).
        if not (authed(req) or (req.client and req.client.host == "127.0.0.1" and not req.headers.get("x-forwarded-for"))):
            return JSONResponse({"error": "unauthorised"}, status_code=403)
        from . import alerts
        h = await asyncio.to_thread(alerts.health)
        return JSONResponse(h, status_code=200 if h["ok"] else 503)
    for base in ("", "/checkin"):
        routes.append(Route(f"{base}/api/health", health))
    # Compressed (Nov 3): the view is about 226 KB of JSON, about 30 KB gzipped, on a phone.
    from starlette.middleware import Middleware
    from starlette.middleware.gzip import GZipMiddleware
    return Starlette(routes=routes, lifespan=lifespan,
                     middleware=[Middleware(GZipMiddleware, minimum_size=1024)])


def main() -> None:
    import uvicorn
    from .tasks import shortsteps
    shortsteps.ENABLED = True   # only the live server summarises steps (Nov 3)
    from .tasks import estimates
    estimates.MODEL = True      # and asks the estimator's model (Tasks v2, Nov 6)
    uvicorn.run(make_app(), host="127.0.0.1", port=PORT, log_level="warning")
