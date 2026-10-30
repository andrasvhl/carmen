"""plan-today delta fetch: raw new WhatsApp and Gmail since a timestamp, as JSON.

The 08:00 pre-run (plan_today_prerun) judges the last 10 days with one model
call. When the owner opens the panel later, the morning session needs only what
arrived after that run. This fetches exactly that, with no model involved, so a
single model call can judge the delta afterwards. Target: under ~10 s.

    ~/automation/bin/run-job plan_today_delta --since 2026-10-06T17:28-07:00

Prints ONE JSON document to stdout (nothing else goes to stdout):

    {"since", "generated",
     "whatsapp": [{"chat", "chat_id", "messages": [{"t", "date", "from", "me", "text"}]}],
     "gmail": [{"account", "thread_id", "subject",
                "messages": [{"t", "date", "from", "to", "text"}]}],
     "chats": [{"chat", "chat_id", "network", "group", "messages": [{"t", "date", "from", "me", "text", "id"}]}],
               (LinkedIn, Messenger, Instagram through Beeper, beeper-bridge.md)
     "counts": {...}, "errors": [...]}

Only chats and threads with a message at or after --since are included, and only
those messages. A failed source or chat goes into "errors"; the rest still prints.

## WhatsApp (whatsapp-bridge.md § Reading WhatsApp, binding)

- Every chat id from a fully paged overview (offset until empty). No chat is
  pre-selected by lastMessage.timestamp, which is absent on some chats.
- The window goes on each messages call (filter.timestamp.gte, epoch computed in
  code from the timezone-aware --since), and is re-checked in code.
- Never keyword-filtered. Missing fields stay missing: a sender with no push name
  in a group is null with its id in "from_id"; a message with neither text nor
  media is "[no text]", not "[media]".
- status@broadcast is skipped. Chats are fetched on a 16-worker thread pool.
- The list endpoint hides roughly a third of group messages (GOWS is_real bug,
  whatsapp-bridge rule 5). Those are merged in from the store, read-only, by
  runner/waha_store.py, deduplicated on the bare message id. Rows the bug
  overwrote beyond recovery are counted in counts.whatsapp, never guessed.
- Edits are applied (whatsapp-bridge rule 6): GOWS stores them encrypted and
  serves the pre-edit text. The text becomes the edited one with "[edited HH:MM]"
  appended, or "[edited HH:MM, new text unreadable]" after the old one.

## Gmail

Both mailboxes (MAILBOXES in plan_today_prerun), concurrently, with that job's
read-only in-memory token refresh. Query after:<epoch> -in:spam -in:trash
-in:drafts, re-checked against internalDate. Body is the text/plain part, else
the HTML part stripped to text.
"""

import argparse
import base64
import html
import json
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from jobs.plan_today_prerun import MAILBOXES, TZ, _gget, _gmail_token, waha_get
from runner import waha_store

WA_WORKERS = 16
GMAIL_WORKERS = 8
OVERVIEW_PAGE = 500
OVERVIEW_PARALLEL = 2
MSG_PAGE = 200
MAX_MSG_PAGES = 10          # 2,000 messages in one chat since the cutoff is far beyond a morning
MAX_MAILS = 300
WA_CHARS = 600
MAIL_CHARS = 800
SKIP_CHATS = {"status@broadcast"}


def _clip(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _stamp(epoch: float) -> tuple[str, str]:
    d = datetime.fromtimestamp(epoch, TZ)
    return d.strftime("%H:%M"), d.strftime("%Y-%m-%d")


# ---------------------------------------------------------------- WhatsApp

def _overview() -> list[dict]:
    """Paged until a page comes back empty (limit is a cap, not "all"). Pages are
    fetched OVERVIEW_PARALLEL at a time: ~1.8 s instead of ~2.5 s for 704 chats."""
    chats, off = [], 0
    with ThreadPoolExecutor(OVERVIEW_PARALLEL) as ex:
        while True:
            offs = [off + k * OVERVIEW_PAGE for k in range(OVERVIEW_PARALLEL)]
            pages = list(ex.map(lambda o: waha_get(
                f"default/chats/overview?limit={OVERVIEW_PAGE}&offset={o}"), offs))
            for page in pages:
                chats += page
            if any(len(p) == 0 for p in pages):
                return chats
            off = offs[-1] + OVERVIEW_PAGE


def _chat_messages(chat_id: str, lo: int) -> list[dict]:
    out = []
    for p in range(MAX_MSG_PAGES):
        page = waha_get(f"default/chats/{urllib.parse.quote(chat_id)}/messages"
                        f"?limit={MSG_PAGE}&offset={p * MSG_PAGE}&downloadMedia=false"
                        f"&filter.timestamp.gte={lo}")
        out += page
        if len(page) < MSG_PAGE:
            break
    return out


def _wa_msg(m: dict, chat_id: str, chat_name) -> dict:
    info = (m.get("_data") or {}).get("Info") or {}
    t, d = _stamp(m["timestamp"])
    me = bool(m.get("fromMe"))
    body = m.get("body") or ""
    text = _clip(body, WA_CHARS) if body.strip() else ("[media]" if m.get("hasMedia") else "[no text]")
    row = {"t": t, "date": d, "from": None, "me": me, "text": text}
    if me:
        row["from"] = "me"
    elif info.get("PushName"):
        row["from"] = info["PushName"]
    elif not chat_id.endswith("@g.us") and chat_name:
        row["from"] = chat_name          # a direct chat: the chat is the sender
    else:
        row["from_id"] = m.get("participant") or info.get("Sender") or m.get("from")
    return row


def whatsapp(lo: int, errors: list) -> tuple[list[dict], dict]:
    t0 = time.monotonic()
    chats = list({c["id"]: c for c in _overview()
                  if c.get("id") and c["id"] not in SKIP_CHATS}.values())
    t_over = time.monotonic() - t0
    try:
        hidden, store = waha_store.hidden_messages(lo)
    except Exception as e:  # noqa: BLE001 - the API half still runs; the gap is reported
        hidden, store = {}, {"hidden_recovered": "FAILED"}
        errors.append({"source": "whatsapp", "error": "hidden-message read from the GOWS store failed, "
                       f"about a third of group messages may be missing: {type(e).__name__}: {e}"[:300]})
    try:
        edited, ed_stats = waha_store.edits(lo)
        store |= ed_stats
    except Exception as e:  # noqa: BLE001
        edited = {}
        store["edits"] = "FAILED"
        errors.append({"source": "whatsapp", "error": "edit read from the GOWS store failed, edited "
                       f"messages show their old text: {type(e).__name__}: {e}"[:300]})
    known = {c["id"] for c in chats}
    chats += [{"id": cid, "name": None} for cid in hidden if cid not in known and cid not in SKIP_CHATS]

    def one(c):
        try:
            msgs = [m for m in _chat_messages(c["id"], lo) + hidden.get(c["id"], [])
                    if isinstance(m.get("timestamp"), (int, float)) and m["timestamp"] >= lo]
        except Exception as e:  # noqa: BLE001 - one dead chat is reported, not fatal
            errors.append({"source": "whatsapp", "chat_id": c["id"], "chat": c.get("name"),
                           "error": f"{type(e).__name__}: {e}"[:300]})
            return None
        if not msgs:
            return None
        seen, rows = set(), []
        for m in sorted(msgs, key=lambda m: m["timestamp"]):
            # REST ids are "<fromMe>_<chat>_<ID>[_<participant>]", store ids the bare ID.
            mid = ((m.get("_data") or {}).get("Info") or {}).get("ID") or m.get("id")
            if mid in seen:
                continue
            seen.add(mid)
            row = _wa_msg(m, c["id"], c.get("name"))
            if mid in edited:
                e = edited[mid]
                when = _stamp(e["timestamp"])[0]
                row["text"] = (_clip(e["text"], WA_CHARS) + f" [edited {when}]" if e["text"] is not None
                               else row["text"] + f" [edited {when}, new text unreadable]")
                row["edited"] = when
            rows.append(row)
        return {"chat": c.get("name"), "chat_id": c["id"], "messages": rows,
                "_last": max(m["timestamp"] for m in msgs)}

    with ThreadPoolExecutor(WA_WORKERS) as ex:
        res = [r for r in ex.map(one, chats) if r]
    res.sort(key=lambda r: -r["_last"])
    for r in res:
        del r["_last"]
    return res, {"chats_scanned": len(chats), "chats": len(res),
                 "messages": sum(len(r["messages"]) for r in res), **store,
                 "seconds_overview": round(t_over, 2), "seconds": round(time.monotonic() - t0, 2)}


# ---------------------------------------------------------------- Gmail

def _hdr(m: dict) -> dict:
    return {h["name"].lower(): h["value"] for h in (m.get("payload") or {}).get("headers", [])}


def _parts(p: dict):
    yield p
    for q in p.get("parts") or []:
        yield from _parts(q)


def _decode(p: dict) -> str:
    data = (p.get("body") or {}).get("data")
    if not data:
        return ""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def _body(m: dict) -> str:
    parts = list(_parts(m.get("payload") or {}))
    for mime in ("text/plain", "text/html"):
        for p in parts:
            if p.get("mimeType") == mime and not (p.get("filename") or ""):
                txt = _decode(p)
                if txt.strip():
                    if mime == "text/html":
                        txt = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", txt)
                        txt = html.unescape(re.sub(r"<[^>]+>", " ", txt))
                    return txt
    return m.get("snippet") and html.unescape(m["snippet"]) or ""


def gmail(account: str, lo: int, errors: list) -> tuple[list[dict], dict]:
    t0 = time.monotonic()
    tok = _gmail_token(account)
    q = urllib.parse.quote(f"after:{lo} -in:spam -in:trash -in:drafts")
    ids, page = [], ""
    while len(ids) < MAX_MAILS:
        res = _gget(tok, f"messages?q={q}&maxResults=100" + (f"&pageToken={page}" if page else ""))
        ids += [m["id"] for m in res.get("messages", [])]
        page = res.get("nextPageToken", "")
        if not page:
            break
    truncated = len(ids) > MAX_MAILS

    def one(i):
        try:
            return _gget(tok, f"messages/{i}?format=full")
        except Exception as e:  # noqa: BLE001
            errors.append({"source": "gmail", "account": account, "message_id": i,
                           "error": f"{type(e).__name__}: {e}"[:300]})
            return None

    with ThreadPoolExecutor(GMAIL_WORKERS) as ex:
        msgs = [m for m in ex.map(one, ids[:MAX_MAILS]) if m]
    threads: dict[str, dict] = {}
    for m in sorted(msgs, key=lambda m: int(m["internalDate"])):
        ts = int(m["internalDate"]) / 1000
        if ts < lo:
            continue
        h = _hdr(m)
        t, d = _stamp(ts)
        th = threads.setdefault(m["threadId"], {"account": account, "thread_id": m["threadId"],
                                                "subject": h.get("subject"), "messages": [],
                                                "_last": 0})
        th["messages"].append({"t": t, "date": d, "from": h.get("from"), "to": h.get("to"),
                               "text": _clip(_body(m), MAIL_CHARS)})
        th["_last"] = ts
    out = sorted(threads.values(), key=lambda r: -r["_last"])
    for r in out:
        del r["_last"]
    c = {"threads": len(out), "messages": sum(len(r["messages"]) for r in out),
         "seconds": round(time.monotonic() - t0, 2)}
    if truncated:
        c["truncated_at"] = MAX_MAILS
        errors.append({"source": "gmail", "account": account,
                       "error": f"more than {MAX_MAILS} messages since cutoff; oldest beyond cap not read"})
    return out, c


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", required=True, help="ISO timestamp with offset, e.g. 2026-10-06T17:28-07:00")
    a = ap.parse_args()
    since = datetime.fromisoformat(a.since)
    if since.tzinfo is None:
        ap.error("--since needs a UTC offset (e.g. -07:00); a naive time is ambiguous")
    lo = int(since.timestamp())
    t0 = time.monotonic()
    errors: list = []
    counts: dict = {}
    wa, mail = [], []

    def run_wa():
        try:
            r, c = whatsapp(lo, errors)
            wa.extend(r)
            counts["whatsapp"] = c
        except Exception as e:  # noqa: BLE001
            counts["whatsapp"] = "FAILED"
            errors.append({"source": "whatsapp", "error": f"{type(e).__name__}: {e}"[:300]})

    def run_mail(acct):
        try:
            r, c = gmail(acct, lo, errors)
            mail.append((acct, r))
            counts[acct] = c
        except Exception as e:  # noqa: BLE001
            counts[acct] = "FAILED"
            errors.append({"source": "gmail", "account": acct, "error": f"{type(e).__name__}: {e}"[:300]})

    cal = {"text": ""}
    bp: list = []

    def run_beeper():
        try:
            from runner import beeper  # lazy: only the box has the CLI
            bp.extend(beeper.recent(since))
            counts["chats"] = len(bp)
        except Exception as e:  # noqa: BLE001
            counts["chats"] = "FAILED"
            errors.append({"source": "beeper", "error": f"{type(e).__name__}: {e}"[:300]})

    def run_cal():
        # The calendar for the day plan, fetched with the pre-run's read-only token so the session
        # needs no per-calendar tool calls (2026-10-06: the context agent spent 18 calls on it).
        try:
            from jobs.plan_today_prerun import calendar as cal_fetch  # lazy: heavy module
            cal["text"], counts["calendar_events"] = cal_fetch(datetime.now(TZ).date())
        except Exception as e:  # noqa: BLE001
            counts["calendar_events"] = "FAILED"
            errors.append({"source": "calendar", "error": f"{type(e).__name__}: {e}"[:300]})

    with ThreadPoolExecutor(3 + len(MAILBOXES)) as ex:
        futs = [ex.submit(run_wa), ex.submit(run_cal), ex.submit(run_beeper)] + [ex.submit(run_mail, acct) for acct in MAILBOXES]
        for f in futs:
            f.result()
    order = {acct: i for i, acct in enumerate(MAILBOXES)}
    gm = [t for _, r in sorted(mail, key=lambda x: order[x[0]]) for t in r]
    counts["seconds"] = round(time.monotonic() - t0, 2)
    doc = {"since": since.isoformat(timespec="minutes"),
           "generated": datetime.now(TZ).isoformat(timespec="seconds"),
           "whatsapp": wa, "gmail": gm, "chats": bp, "calendar": cal["text"], "counts": counts, "errors": errors}
    sys.stdout.write(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
