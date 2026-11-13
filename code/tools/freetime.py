#!/usr/bin/env python3
"""Find the weekday evenings when most of a WhatsApp group is free.

  vt freetime --auth                       one-time Google sign-in (free/busy + Gmail read)
  vt freetime "<GROUP_NAME>"        next 14 days, weekday dinners, 18:30-22:30, 2h
  vt freetime <group> --days 7 --start 19:00 --end 22:00 --duration 90
  vt freetime <group> --from 2026-10-05 --weekends
  vt freetime <group> --no-save            do not write newly found emails to the CRM
  vt freetime <group> --json

One pass, three sources:
  1. WhatsApp (WAHA on vault-host): the group's members, as phone numbers.
  2. Identity: the CRM (Areas/People) by phone, then by name; Gmail headers as the
     fallback for anyone the CRM cannot place. Gmail finds are written back to the
     CRM, so the next run of any group containing that person is a pure lookup.
  3. Google Calendar freeBusy: every member's busy blocks in a single request.

Free/busy cannot tell "free" from "calendar hidden". A member with no busy block
anywhere in the window (daytime included) is reported as blank, not as free, and
is left out of the counts. Stdlib only, like the Canvas pull.
"""
import argparse
import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import subprocess
import sys
import unicodedata
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from email.utils import getaddresses
from pathlib import Path
from zoneinfo import ZoneInfo

from runner.config import VAULT

PEOPLE = Path(VAULT) / "Areas" / "People"
TOKEN = Path.home() / ".config" / "vt" / "google.json"
CLIENT = Path.home() / ".google_workspace_mcp" / "client_secret.json"
SCOPES = ["https://www.googleapis.com/auth/calendar.freebusy",
          "https://www.googleapis.com/auth/gmail.readonly",
          "openid", "email"]
ACCOUNT = "<YOUR_EMAIL>"
DOMAIN = "stanford.edu"
STEP = timedelta(minutes=15)


# ---------------------------------------------------------------- Google auth

def _client():
    d = json.loads(CLIENT.read_text())
    return d.get("installed") or d.get("web")


def auth():
    """Loopback OAuth for a desktop client. Writes TOKEN, never touches the gws MCP's file."""
    c = _client()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    got = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if "code" in q or "error" in q:
                got.update(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"vt freetime: done, you can close this tab.")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    redirect = f"http://localhost:{srv.server_port}"
    url = c["auth_uri"] + "?" + urllib.parse.urlencode({
        "client_id": c["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent",
        "login_hint": ACCOUNT, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    print("Opening Google sign-in. If no browser opens, visit:\n" + url, file=sys.stderr)
    webbrowser.open(url)
    while not got:
        srv.handle_request()
    if got.get("error") or got.get("state") != state:
        sys.exit(f"auth failed: {got.get('error', 'state mismatch')}")
    tok = _post(c["token_uri"], {
        "code": got["code"], "client_id": c["client_id"], "client_secret": c["client_secret"],
        "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier})
    if "refresh_token" not in tok:
        sys.exit("auth failed: Google returned no refresh token")
    granted = set(tok.get("scope", "").split())
    missing = [s for s in SCOPES[:2] if s not in granted]
    _save_token({"refresh_token": tok["refresh_token"], "access_token": tok["access_token"],
                 "expiry": (datetime.now() + timedelta(seconds=tok.get("expires_in", 3600) - 60)).isoformat(),
                 "scopes": sorted(granted)})
    if missing:
        sys.exit(f"signed in, but Google did not grant: {', '.join(missing)}. "
                 "Stanford may block this scope for the app.")
    print(f"ok: token saved to {TOKEN}")


def _save_token(d):
    TOKEN.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1))
    os.chmod(tmp, 0o600)
    os.replace(tmp, TOKEN)


def access_token():
    if not TOKEN.exists():
        sys.exit("no Google token yet: run `vt freetime --auth` once")
    t = json.loads(TOKEN.read_text())
    if t.get("access_token") and datetime.fromisoformat(t["expiry"]) > datetime.now():
        return t["access_token"]
    c = _client()
    r = _post(c["token_uri"], {"client_id": c["client_id"], "client_secret": c["client_secret"],
                               "refresh_token": t["refresh_token"], "grant_type": "refresh_token"})
    if "access_token" not in r:
        sys.exit(f"Google token refresh failed ({r.get('error')}): run `vt freetime --auth`")
    t.update(access_token=r["access_token"],
             expiry=(datetime.now() + timedelta(seconds=r.get("expires_in", 3600) - 60)).isoformat())
    _save_token(t)
    return t["access_token"]


def _post(url, form):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode())
    try:
        return json.load(urllib.request.urlopen(req, timeout=20))
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}")


def _api(tok, url, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {tok}",
                                          "Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read())["error"]["message"]
        except Exception:
            msg = e.reason
        raise SystemExit(f"Google API {e.code}: {msg}")


# ---------------------------------------------------------------- WhatsApp

# Runs on whichever machine holds ~/.waha.env: locally on vault-host, over one ssh
# call from the Mac. Everything WhatsApp-side happens inside this single round trip.
_REMOTE = r'''
import json, os, re, sys, unicodedata, urllib.request, urllib.parse
env = dict(re.findall(r"^\s*(?:export\s+)?(\w+)=[\"']?([^\"'\n]*)", open(os.path.expanduser("~/.waha.env")).read(), re.M))
BASE, KEY = "http://127.0.0.1:3000", env["WAHA_API_KEY"]
def get(p):
    return json.load(urllib.request.urlopen(urllib.request.Request(BASE + p, headers={"X-Api-Key": KEY}), timeout=20))
def norm(s):
    s = "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))
    return re.sub(r"\s+", "", s)
q = norm(sys.argv[1])
groups = [c for c in get("/api/default/chats/overview?limit=2000") if c["id"].endswith("@g.us")]
hits = [c for c in groups if q in norm(c.get("name") or "")]
exact = [c for c in hits if norm(c.get("name") or "") == q]
hits = exact or hits
if len(hits) != 1:
    print(json.dumps({"error": "ambiguous" if hits else "no group", "candidates": [c.get("name") for c in hits]})); sys.exit()
g = hits[0]
me = re.sub(r"\D", "", (get("/api/sessions/default/me") or {}).get("id", "").split("@")[0])
out = []
for p in get(f"/api/default/groups/{urllib.parse.quote(g['id'])}/participants"):
    num = (p.get("PhoneNumber") or p.get("JID") or "").split("@")[0]
    if not num or num == me:
        continue
    try:
        c = get(f"/api/contacts?session=default&contactId={num}@c.us")
    except Exception:
        c = {}
    out.append({"phone": "+" + num, "name": (c.get("name") or "").strip(), "pushname": (c.get("pushname") or "").strip()})
print(json.dumps({"group": g.get("name"), "members": out}))
'''


def whatsapp_group(query):
    if (Path.home() / ".waha.env").exists():
        cmd = ["python3", "-", query]
    else:
        cmd = ["ssh", "vault-host", "python3 - " + _shq(query)]
    r = subprocess.run(cmd, input=_REMOTE, capture_output=True, text=True, timeout=90)
    if r.returncode:
        sys.exit(f"WhatsApp read failed: {r.stderr.strip()[-400:]}")
    d = json.loads(r.stdout)
    if "error" in d:
        cands = "\n  ".join(d["candidates"]) or "(none)"
        sys.exit(f"{d['error']} for {query!r}. Matching groups:\n  {cands}")
    return d


def _shq(s):
    return "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------- identity

def fold(s):
    return "".join(c for c in unicodedata.normalize("NFKD", (s or "").lower())
                   if not unicodedata.combining(c)).strip()


def digits(p):
    return re.sub(r"\D", "", p or "")


def crm_load():
    from tools.people import load
    return load()


def crm_match(member, ppl):
    d = digits(member["phone"])
    for p in ppl:
        if any(digits(x) == d for x in p["phone"]):
            return p, "phone"
    name = fold(member["name"])
    if len(name.split()) >= 2:
        for p in ppl:
            if fold(p["name"]) == name or any(fold(a) == name for a in p["aliases"]):
                return p, "name"
    return None, None


def domain_email(emails):
    return next((e for e in emails if e.lower().endswith("@" + DOMAIN)), None)


def gmail_lookup(tok, name):
    """Most frequent @stanford.edu address whose display name carries this first and last name."""
    toks = fold(name).split()
    if len(toks) < 2:
        return None
    first, last = toks[0], toks[-1]
    base = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
    try:
        ids = [m["id"] for m in _api(tok, base + "?" + urllib.parse.urlencode(
            {"q": " OR ".join(f'"{v}"' for v in dict.fromkeys(
                [f"{first} {last}", fold(name), name, f"{name.split()[0]} {name.split()[-1]}"]))
                 # unquoted too: "Eitan Darwish" misses headers that read "Eitan Ziv Darwish"
                 + f" OR ({first} {last})",
             "maxResults": 6})).get("messages", [])]
    except (Exception, SystemExit):
        return None
    votes = {}
    for i in ids:
        try:
            m = _api(tok, f"{base}/{i}?format=metadata&metadataHeaders=From&metadataHeaders=To&metadataHeaders=Cc")
        except (Exception, SystemExit):
            continue
        hdrs = [h["value"] for h in m.get("payload", {}).get("headers", [])]
        for disp, addr in getaddresses(hdrs):
            words = set(fold(disp).replace('"', "").split())
            if addr.lower().endswith("@" + DOMAIN) and first in words and last in words:
                votes[addr.lower()] = votes.get(addr.lower(), 0) + 1
    return max(votes, key=votes.get) if votes else None


def resolve(members, tok):
    ppl = crm_load()
    todo = []
    for m in members:
        p, how = crm_match(m, ppl)
        m["crm"] = p
        m["display"] = p["name"] if p else (m["name"] or m["pushname"] or m["phone"])
        m["email"] = domain_email(p["email"]) if p else None
        m["via"] = f"CRM ({how})" if m["email"] else None
        if m["email"] and how == "name":
            m["save"] = "phone"
        if not m["email"]:
            todo.append(m)
    def look(m):
        full = m["crm"]["name"] if m["crm"] else m["name"]
        return m, (gmail_lookup(tok, full) if full else None)
    with ThreadPoolExecutor(8) as ex:
        for m, e in ex.map(look, todo):
            if e:
                m.update(email=e, via="Gmail", save="email")
    return members


def crm_record(members):
    """Write Gmail finds back to the CRM. Additive only: an email or phone is appended,
    never replaced; a new person note is created only if no file of that name exists."""
    done = []
    today = date.today().isoformat()
    for m in members:
        if not m.get("save"):
            continue
        p = m["crm"]
        if p:
            path = PEOPLE / f"{p['name']}.md"
            field, val = ("crm_email", m["email"]) if m["save"] == "email" else ("crm_phone", m["phone"])
            if _append_list(path, field, val):
                done.append(f"{p['name']}: added {val} to {field}")
        elif len(m["name"].split()) >= 2:
            path = PEOPLE / (re.sub(r'[\\/:#^|*"<>?]', "", m["name"]).strip() + ".md")
            if path.exists():
                continue
            path.write_text(_new_person(m, today), encoding="utf-8")
            done.append(f"{m['name']}: new CRM note ({m['email']}, {m['phone']})")
    return done


def _append_list(path, field, val):
    text = path.read_text(encoding="utf-8")
    head, sep, body = text.partition("\n---\n") if text.startswith("---\n") else ("", "", "")
    if not sep:
        return False
    mo = re.search(rf"^{field}:[ \t]*(.*)$", head, re.M)
    cur = []
    if mo and mo.group(1).strip():
        raw = mo.group(1).strip().strip("[]")
        cur = [x.strip().strip('"').strip("'") for x in raw.split(",") if x.strip()]
    if any(x.lower() == val.lower() for x in cur):
        return False
    line = f"{field}: " + json.dumps(cur + [val], ensure_ascii=False).replace('","', '", "')
    head = head[:mo.start()] + line + head[mo.end():] if mo else head + "\n" + line
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(head + sep + body, encoding="utf-8")
    os.replace(tmp, path)
    return True


def _new_person(m, today):
    return f"""---
type: person
aliases: []
crm_circle: dormant
crm_relation: classmate
crm_org: "Stanford GSB"
crm_cohort:
crm_city:
areas: ["[[MBA]]"]
crm_owed: none
crm_email: ["{m['email']}"]
crm_phone: ["{m['phone']}"]
crm_handles: []
crm_channel: whatsapp
crm_languages: []
crm_birthday:
crm_met:
crm_last_contact:
crm_next_contact:
---

# {m['name']}

**Classmate, Stanford GSB.** Added {today} by `vt freetime` from WhatsApp + Gmail; not yet described.

## Fixed

| Fact | Value | Source | As of |
|---|---|---|---|
| Relation | Classmate | WhatsApp group + Gmail | {today} |

## See also
- [[Areas/People/People|People]] — the database
"""


# ---------------------------------------------------------------- free/busy + scoring

def freebusy(tok, emails, t0, t1, tz):
    out = {}
    for i in range(0, len(emails), 50):
        r = _api(tok, "https://www.googleapis.com/calendar/v3/freeBusy",
                 {"timeMin": t0.isoformat(), "timeMax": t1.isoformat(), "timeZone": tz,
                  "items": [{"id": e} for e in emails[i:i + 50]]})
        for e, c in r.get("calendars", {}).items():
            if c.get("errors"):
                out[e] = None
            else:
                out[e] = [(datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
                           datetime.fromisoformat(b["end"].replace("Z", "+00:00"))) for b in c.get("busy", [])]
    return out


def free(busy, a, b):
    return not any(s < b and e > a for s, e in busy)


def score(people, self_key, days, start, end, dur, tz):
    """Per evening: the slot with the most readable people free, preferring slots you can make."""
    rows = []
    for d in days:
        w0 = datetime.combine(d, start, tz)
        w1 = datetime.combine(d, end, tz)
        best = None
        s = w0
        while s + dur <= w1:
            who = [k for k, b in people.items() if free(b, s, s + dur)]
            key = (self_key in who, len(who))
            if best is None or key > best[0]:
                best = (key, s, who)
            s += STEP
        if not best:
            continue
        _, s, who = best
        a, b = s, s + dur
        while a - STEP >= w0 and all(free(people[k], a - STEP, b) for k in who):
            a -= STEP
        while b + STEP <= w1 and all(free(people[k], a, b + STEP) for k in who):
            b += STEP
        rows.append({"day": d, "start": a, "end": b, "who": who, "you": self_key in who})
    return rows


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("group", nargs="?", help="WhatsApp group name, or any unique part of it")
    ap.add_argument("--auth", action="store_true", help="one-time Google sign-in")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--from", dest="frm", help="first day, YYYY-MM-DD (default: tomorrow)")
    ap.add_argument("--start", default="18:30")
    ap.add_argument("--end", default="22:30")
    ap.add_argument("--duration", type=int, default=120, help="minutes")
    ap.add_argument("--weekends", action="store_true", help="include Sat/Sun")
    ap.add_argument("--tz", default="America/Los_Angeles")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--no-save", action="store_true", help="do not write Gmail finds to the CRM")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.auth:
        return auth()
    if not a.group:
        ap.error("give a group name, or --auth")

    tz = ZoneInfo(a.tz)
    first = date.fromisoformat(a.frm) if a.frm else datetime.now(tz).date() + timedelta(days=1)
    days = [first + timedelta(i) for i in range(a.days)]
    days = [d for d in days if a.weekends or d.weekday() < 5]
    start, end = time.fromisoformat(a.start), time.fromisoformat(a.end)
    dur = timedelta(minutes=a.duration)

    tok = access_token()
    g = whatsapp_group(a.group)
    members = resolve(g["members"], tok)
    recorded = [] if a.no_save else crm_record(members)

    emails = sorted({m["email"] for m in members if m["email"]} | {ACCOUNT})
    t0 = datetime.combine(days[0], time(0), tz)
    t1 = datetime.combine(days[-1] + timedelta(1), time(0), tz)
    fb = freebusy(tok, emails, t0, t1, a.tz)

    by_email = {m["email"]: m for m in members if m["email"]}
    readable, blank, denied = {}, [], []
    for e in emails:
        b = fb.get(e)
        label = "You" if e == ACCOUNT else by_email[e]["display"]
        if b is None:
            denied.append(label)
        elif not b and e != ACCOUNT:
            blank.append(label)
        else:
            readable[label] = b
    unresolved = [m["display"] + ("" if m["name"] else f" ({m['phone']})") for m in members if not m["email"]]
    rows = score(readable, "You", days, start, end, dur, tz)

    if a.json:
        print(json.dumps({"group": g["group"], "members": len(members) + 1, "readable": list(readable),
                          "blank": blank, "no_access": denied, "unresolved": unresolved, "recorded": recorded,
                          "evenings": [{**r, "day": r["day"].isoformat(), "start": r["start"].isoformat(),
                                        "end": r["end"].isoformat()} for r in rows]},
                         ensure_ascii=False, indent=1))
        return

    n = len(readable)
    mine = sorted([r for r in rows if r["you"]], key=lambda r: (-len(r["who"]), -(r["end"] - r["start"]), r["day"]))
    others = sorted([r for r in rows if not r["you"]], key=lambda r: (-len(r["who"]), r["day"]))
    fmt = lambda t: t.strftime("%-I:%M").replace(":00", "") + t.strftime("%p").lower()
    print(f"{g['group']}: {len(members) + 1} members, {n} readable calendars, "
          f"{days[0]:%a %b %-d} to {days[-1]:%a %b %-d}, {a.start}-{a.end}, {a.duration} min\n")
    print("| Evening | Free | Who | Window |\n|---|---|---|---|")
    for r in mine[:a.top]:
        who = ", ".join(x for x in r["who"])
        print(f"| {r['day']:%a %b %-d} | {len(r['who'])}/{n} | {who} | {fmt(r['start'])}-{fmt(r['end'])} |")
    top_mine = len(mine[0]["who"]) if mine else 0
    better = [r for r in others if len(r["who"]) > top_mine][:3]
    for r in better:
        print(f"| {r['day']:%a %b %-d} (you busy) | {len(r['who'])}/{n} | {', '.join(r['who'])} | "
              f"{fmt(r['start'])}-{fmt(r['end'])} |")
    print()
    if blank:
        print(f"Blank calendars, not counted (hidden or unused): {', '.join(blank)}")
    if denied:
        print(f"No calendar access: {', '.join(denied)}")
    if unresolved:
        print(f"No email found, not checked: {', '.join(unresolved)}")
    guessed = [f"{m['display']} via {m['via']}" for m in members if m.get("via") == "Gmail"]
    if guessed:
        print(f"Resolved from Gmail this run: {', '.join(guessed)}")
    if recorded:
        print("Recorded in CRM: " + "; ".join(recorded))


if __name__ == "__main__":
    main()
