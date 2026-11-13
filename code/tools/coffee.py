#!/usr/bin/env python3
"""Find 2-3 good slots for a 1:1 coffee chat with one classmate.

  vt coffee "Jane Doe"                 45 min, next 10 days from tomorrow, 3 options
  vt coffee doe --duration 30
  vt coffee "Jane" --email jane@example.com   skip the lookup
  vt coffee doe --from 2026-10-26 --days 7 --json
  vt coffee smith doe johnson          separate 1:1s in one go, no slot offered twice

Identity: CRM (Areas/People) by name or alias, any unique part; Gmail headers as the
fallback, written back to the CRM like `vt freetime`. Free/busy: the person plus every
calendar that blocks the owner (main, personal Gmail, Luma, Partiful), in one request.

Week shape (Week Structure, autumn 2026): Monday is the solo day and Wednesday the
big-rocks day, so only Tue/Thu/Fri are offered unless --weekdays says otherwise. Window
11:00-17:00. Preferred, in order: right after a class or event (already on campus),
lunch 12:00-13:00, Thursday 15:00-17:00 (the standing free afternoon). One option per day.
"""
import argparse
import json
import sys
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from tools.freetime import (ACCOUNT, STEP, access_token, crm_load, crm_record, domain_email,
                            fold, free, freebusy, gmail_lookup)

BLOCKING = [ACCOUNT, "<YOUR_PERSONAL_EMAIL>",
            "<LUMA_CALENDAR_ID>",   # Luma
            "<PARTIFUL_CALENDAR_ID>"]   # Partiful
DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def find_person(query, tok):
    q = fold(query)
    ppl = crm_load()
    names = lambda p: [fold(p["name"])] + [fold(a) for a in p["aliases"]]
    hits = [p for p in ppl if q in names(p)] or [p for p in ppl if any(q in n for n in names(p))]
    if len(hits) > 1:
        sys.exit("ambiguous: " + "; ".join(p["name"] for p in hits[:15]))
    if hits:
        p = hits[0]
        e = domain_email(p["email"]) or (p["email"][0] if p["email"] else None)
        if e:
            return p["name"], e, "CRM", []
        e = gmail_lookup(tok, p["name"])
        if not e:
            sys.exit(f"{p['name']} is in the CRM but has no email, and Gmail found none: pass --email")
        rec = crm_record([{"crm": p, "save": "email", "email": e, "phone": "", "name": p["name"]}])
        return p["name"], e, "Gmail", rec
    e = gmail_lookup(tok, query)
    if not e:
        sys.exit(f"no CRM note or Gmail match for {query!r}: give the full name or pass --email")
    return query, e, "Gmail (not in CRM)", []


def options(me, them, days, start, end, dur, tz, n):
    """Best slot per day, scored by fit with the week; then the n best days."""
    per_day = []
    for d in days:
        w0, w1 = datetime.combine(d, start, tz), datetime.combine(d, end, tz)
        ends = {e for _, e in me}
        starts = {s for s, _ in me}
        best = None
        s = w0
        while s + dur <= w1:
            if free(me, s, s + dur) and free(them, s, s + dur):
                score, why = 0, []
                if s in ends or s - STEP in ends:
                    score += 3; why.append("right after your last block")
                elif s + dur in starts:
                    score += 2; why.append("right before your next block")
                if time(12) <= s.time() and (s + dur).time() <= time(13):
                    score += 2; why.append("lunch")
                if d.weekday() == 3 and s.time() >= time(15):
                    score += 1; why.append("Thursday free afternoon")
                if s.time() < time(13) or s.time() >= time(15):
                    score += 0.5                                   # outside the 13-15 dip
                if best is None or score > best[0]:
                    best = (score, s, why)
            s += STEP
        if best:
            per_day.append({"day": d, "start": best[1], "end": best[1] + dur,
                            "score": best[0] - 0.1 * (d - days[0]).days, "why": best[2]})
    return sorted(sorted(per_day, key=lambda r: -r["score"])[:n], key=lambda r: r["start"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("person", nargs="+", help="classmate's name, alias, or any unique part of it")
    ap.add_argument("--email", help="skip the lookup (one person only)")
    ap.add_argument("--duration", type=int, default=45, choices=[30, 45, 60], help="minutes")
    ap.add_argument("--days", type=int, default=10, help="calendar days to search")
    ap.add_argument("--from", dest="frm", help="first day, YYYY-MM-DD (default: tomorrow)")
    ap.add_argument("--weekdays", default="tue,thu,fri", help="e.g. tue,wed,thu,fri")
    ap.add_argument("--start", default="11:00")
    ap.add_argument("--end", default="17:00")
    ap.add_argument("--options", type=int, default=3)
    ap.add_argument("--tz", default="America/Los_Angeles")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    tz = ZoneInfo(a.tz)
    allowed = {DAYS[x.strip()[:3].lower()] for x in a.weekdays.split(",")}
    first = date.fromisoformat(a.frm) if a.frm else datetime.now(tz).date() + timedelta(days=1)
    days = [first + timedelta(i) for i in range(a.days) if (first + timedelta(i)).weekday() in allowed]
    if not days:
        sys.exit("no allowed weekdays in that range")

    tok = access_token()
    if a.email and len(a.person) > 1:
        sys.exit("--email works with one person only")
    people, errors = [], []
    for q in a.person:
        if a.email:
            people.append((q, a.email, "given", []))
            continue
        try:
            people.append(find_person(q, tok))
        except SystemExit as e:
            errors.append(f"{q}: {e}")
    if errors:
        sys.exit("\n".join(errors))

    t0 = datetime.combine(days[0], time(0), tz)
    t1 = datetime.combine(days[-1] + timedelta(1), time(0), tz)
    fb = freebusy(tok, BLOCKING + [p[1] for p in people], t0, t1, a.tz)
    me = [b for c in BLOCKING for b in (fb.get(c) or [])]
    status = {p[1]: "no access" if fb.get(p[1]) is None else ("blank" if not fb.get(p[1]) else "ok") for p in people}
    win = (time.fromisoformat(a.start), time.fromisoformat(a.end), timedelta(minutes=a.duration), tz, a.options)

    groups, taken = [], []
    for p in people:                     # earlier people's options are blocked for later ones
        rows = options(me + taken, fb.get(p[1]) or [], days, *win)
        taken += [(r["start"], r["end"]) for r in rows]
        groups.append(([p], rows))

    if a.json:
        print(json.dumps({"chats": [
            {"people": [{"name": n, "email": e, "via": v, "calendar": status[e], "recorded": r} for n, e, v, r in ps],
             "options": [{"start": r["start"].isoformat(), "end": r["end"].isoformat(), "why": r["why"]} for r in rows]}
            for ps, rows in groups]}, ensure_ascii=False, indent=1))
        return
    fmt = lambda t: t.strftime("%-I:%M").replace(":00", "") + t.strftime("%p").lower()
    print(f"{a.duration} min, {days[0]:%a %b %-d} to {days[-1]:%a %b %-d}, {a.weekdays}, {a.start}-{a.end}"
          + (f", {len(people)} separate 1:1s" if len(people) > 1 else ""))
    for ps, rows in groups:
        print("\n" + ", ".join(f"{n} <{e}> via {v}" for n, e, v, _ in ps) + "\n")
        print("| # | Day | Time | Why |\n|---|---|---|---|")
        for i, r in enumerate(rows, 1):
            print(f"| {i} | {r['day']:%a %b %-d} | {fmt(r['start'])}-{fmt(r['end'])} | {', '.join(r['why']) or 'free for all'} |")
        if not rows:
            print("| - | none | | widen --days, --weekdays or --start/--end |")
    for n, e, _, rec in people:
        if status[e] == "blank":
            print(f"\n{n}'s calendar shows no busy blocks at all: probably hidden, so slots ignore it.")
        elif status[e] == "no access":
            print(f"\nNo access to {n}'s calendar: slots ignore it.")
        if rec:
            print("Recorded in CRM: " + "; ".join(rec))


if __name__ == "__main__":
    main()
