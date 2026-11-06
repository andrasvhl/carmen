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
                 "tomorrow": {**day, "date": (dt.datetime.now(clock.TZ).date() + dt.timedelta(days=1)).isoformat()},
                 "prerun": {"running": False, "last_ok": None}, "replies": [], "claude": [],
                 "counts": {"today": 0, "replies": 0, "claude": 0}, "notes": []}
        v["notes"] = ["The morning list could not be read, so only what is safe shows. It is logged; a Refresh or the next pre-run fixes it."] + v["notes"]
        return v

    def find(row_id: str) -> dict | None:
        return next((r for r in planning_read.all_rows(current_view()) if r["id"] == row_id), None)

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
                v["plan_pending"] = planning_read.plan_pending(dt.datetime.now(clock.TZ).date())
            except Exception as exc:   # a reminder, never a broken view
                taplog.log_event("error", where="plan_pending", detail=repr(exc)[:200])
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
            hub_box["reaper"] = asyncio.create_task(hub_box["hub"].housekeeping())
        return hub_box["hub"]

    def chat_key(kind: str, row_id: str, day_name: str) -> str:
        if kind == "plan":
            return "plan:" + (dt.datetime.now(clock.TZ).date() + dt.timedelta(days=1 if day_name == "tomorrow" else 0)).isoformat()
        return "task:" + row_id

    async def day_view(req: Request):
        """The day's calendar for the Plan chat's day view (Oct 31)."""
        if not authed(req):
            return JSONResponse({"error": "unauthorised"}, status_code=403)
        now = dt.datetime.now(clock.TZ)
        d = now.date() + dt.timedelta(days=1 if req.query_params.get("day") == "tomorrow" else 0)
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
        if q.get("kind", "task") != "plan" and rid:   # the page may still hold the card's title
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
        if kind != "plan" and row_id:
            # A chat on a card without a note yet creates it (Nov 4), so the chat keys on the note;
            # Stop and approve find that same chat from the title the page still holds.
            row_id = await asyncio.to_thread(card_id, row_id, dry or op != "send")
            if not dry:
                from .tasks import cardnotes
                await asyncio.to_thread(cardnotes.touch, row_id)
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
        return await send_message(req, h, body, key, kind, row_id, day_name, dry, tab_name, first)

    async def send_message(req, h, body, key, kind, row_id, day_name, dry, tab_name, first, voice=False):
        from .chat import Busy
        text = str(body.get("text", ""))[:4000].strip()
        images = web.chat_images(body, config.VAULT)
        base_day = dt.datetime.now(clock.TZ).date() + dt.timedelta(days=1 if day_name == "tomorrow" else 0)
        context, row = "", None
        if kind == "plan":
            title = f"Plan {clock._label(base_day)}"
            if not text and not images:
                ids, over = planning_read.plan_ids(body)
                # Each planned card is a note first, so the plan names its ID and the block it
                # gets carries a Task: line (Nov 4: Pal's call sat unslotted under a batch block).
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
                text = planning_read.plan_text(base_day, picked, ending=False) + planning_read.plan_gaps_note(
                    over, [i for i in dict.fromkeys(ids) if i not in seen], "plan ids (chat)")
            if first:
                taplog.log_event("tap", action="plan", id="", test=dry, src=source(req), tab=tab_name, edited=False)
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
                return JSONResponse(web.link_vault_files(h.send(key, title, text, context=context, test=dry, kind=kind,
                                           prompt_text=frame_transcript(text), voice=True), secret))
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
        body = {"text": text, "ids": q.getlist("ids"), "from": q.get("from", "")}
        day_name = "tomorrow" if q.get("day") == "tomorrow" else "today"
        kind, row_id = q.get("kind", "task"), q.get("id", "")
        if kind != "plan" and row_id:
            row_id = await asyncio.to_thread(card_id, row_id, q.get("test") == "1")
            if q.get("test") != "1":
                from .tasks import cardnotes
                await asyncio.to_thread(cardnotes.touch, row_id)
        return await send_message(req, h, body, chat_key(kind, row_id, day_name), kind, row_id, day_name,
                            q.get("test") == "1", q.get("tab", ""), q.get("first") == "1", voice=True)

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
            return JSONResponse(await asyncio.to_thread(meditation_entry.journal_entry, dt.datetime.now(clock.TZ).date()))
        except OSError as exc:   # the template moved or the entry is unreadable: say which, never a 500
            taplog.log_event("error", where="journal", detail=repr(exc)[:200])
            return JSONResponse({"error": f"the meditation could not be read ({getattr(exc, 'filename', '') or exc})"},
                                status_code=503)

    from .direction.goals import routes as goals_routes   # the Goals tab (Nov 5), jobs/checkin_app/goals.py
    routes = []
    for base in ("", "/checkin"):   # with or without the Funnel stripping its path
        routes += [Route(f"{base}/", page), Route(f"{base}/api/view", view),
                   Route(f"{base}/api/journal", journal), Route(f"{base}/file", vault_file),
                   Route(f"{base}/login", login, methods=["GET", "POST"]),
                   *[Route(f"{base}/{name}", static) for name in STATIC],
                   Route(f"{base}/api/act", act, methods=["POST"]),
                   Route(f"{base}/api/chat", chat_get), Route(f"{base}/api/chat", chat_post, methods=["POST"]),
                   Route(f"{base}/api/chat/voice", chat_voice, methods=["POST"]),
                   Route(f"{base}/api/chat/image", chat_image, methods=["POST"]),
                   Route(f"{base}/api/day", day_view),
                   *goals_routes(base, authed)]
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):   # Starlette 1.x: no on_shutdown; warm Claude clients close here
        import asyncio

        async def outcomes_loop():   # the server runs all day, so it keeps the 23:50 snapshot itself
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
        task = asyncio.create_task(outcomes_loop())
        watcher = asyncio.create_task(watch_loop())
        ticker = asyncio.create_task(tick_loop())
        yield
        task.cancel()
        watcher.cancel()
        ticker.cancel()
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
