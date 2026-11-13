"""Twice a day: re-price every watched trip and push when a fare is a deal.

Built 2026-10-14 with the `travel` skill. The owner asked for alerts "if the
flight prices indicate that there might be a good deal", nothing else, so this
pushes deals only. Prices going up are visible on the status page, never
pushed.

Where watches come from: a ```flight-watch block, JSON, inside a trip page
under Areas/Personal/Travel/. The skill writes the block when its verdict is
"wait"; the owner can edit or delete it like any other text. This job only reads
those pages; it never writes to them.

    ```flight-watch
    {"id": "example-dec", "from": ["AAA", "BBB"], "to": ["CCC"],
     "dates": "2027-04-09..2027-04-11", "stops": 2, "bag": true,
     "target_usd": 700, "until": "2026-11-30"}
    ```

    id          stable slug; price history is keyed on it
    from, to    IATA codes, every airport worth considering at each end
    dates       departure days: YYYY-MM-DD, a comma list, or A..B
    return      optional return date, for a round trip
    stops       optional max stops (0, 1, 2)
    adults      optional passenger count (default 1); prices are totals
    bag         true if he needs a checked bag: hidden-city fares are ignored
    target_usd  optional: push at or below this price
    until       optional: stop watching after this day (default: last departure day)

A fare is a deal when it is at or under target_usd, or, once four or more
checks exist, at least 12 % and $40 under the median of what this watch has
seen, or a new low by 5 %. A deal pushes once; it pushes again only if the
price falls another 5 %, or after seven days if it is still a deal. A big
hidden-city saving (30 % and $100 under the best regular fare) is mentioned in
the push when the watch has no bag.

Everything it sees goes to _system/flight-watch-status.md, rewritten each run,
which is where the skill reads the trend from.

    ssh vault-host '~/automation/bin/run-job flight_watch --dry-run'
"""

import json
import re
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from runner import clock, config, notify, quiet_hours
from runner.job import Job, log, IMPORTANT, ROUTINE, YOU, AUTOMATIC
from tools import flights

TRIPS = config.VAULT / "Areas" / "Personal" / "Travel"
STATUS_PAGE = config.VAULT / "_system" / "flight-watch-status.md"
BLOCK = re.compile(r"```flight-watch\s*\n(.*?)\n```", re.S)
MAX_COMBOS_PER_RUN = 30     # Skiplagged allows ~7 calls a minute; see tools/flights.py
HISTORY_KEEP = 120          # checks per watch, about two months at two a day
COVERS = "flight-watch"


def main() -> int:
    with Job("flight_watch", description="Flight price watcher",
             watches=(COVERS,)) as job:
        watches, problems = load_watches()
        job.check("watch blocks parse", ok=not problems, detail="; ".join(problems)[:600],
                  tier=IMPORTANT, owner=YOU, key="parse", covers=COVERS)
        memory = job.recall()
        history: dict = memory.get("history", {})
        alerts: dict = memory.get("alerts", {})
        pending: list = memory.get("pending", [])

        today = clock.today()   # his day, Pacific (audit A4)
        active = [w for w in watches if w["until"] >= today]
        job.record("watches active", len(active))

        # Oldest-checked first, so a run that hits the budget rotates fairly.
        active.sort(key=lambda w: (history.get(w["id"]) or [{"t": ""}])[-1]["t"])
        budget, checked, failed, deals = MAX_COMBOS_PER_RUN, 0, [], 0
        for w in active:
            days = [d for d in w["days"] if d >= today.isoformat()]
            combos = len(w["from"]) * len(w["to"]) * len(days)
            if not days or combos > budget:
                continue
            budget -= combos
            offers, errors = flights.search_grid(
                w["from"], w["to"], days, return_date=w.get("return"),
                hidden=not w["bag"], max_stops=w.get("stops"), adults=w["adults"])
            checked += 1
            if not offers:
                failed.append(f"{w['id']}: no fares ({'; '.join(errors[:2]) or 'empty'})")
                continue
            obs = observe(w, offers)
            series = history.setdefault(w["id"], [])
            verdict = judge(w, obs, series, alerts.get(w["id"]))
            series.append(obs)
            del series[:-HISTORY_KEEP]
            log(f"flight_watch: {w['id']} best {obs['best']:.0f} USD "
                f"({obs['route']} {obs['date']}) -> {verdict or 'no deal'}")
            if verdict:
                deals += 1
                pending.append(push_text(w, obs, series, verdict))
                alerts[w["id"]] = {"price": obs["best"], "t": obs["t"]}

        job.check("flight sources answer", ok=not failed, detail="; ".join(failed)[:600],
                  tier=ROUTINE, owner=AUTOMATIC, key="sources", covers=COVERS)
        job.record("watches checked", checked)
        job.record("deals found", deals)

        pending = deliver(job, pending)
        if not job.dry_run:
            write_status_page(watches, history, alerts, job)
        job.remember({"history": history, "alerts": alerts, "pending": pending})
    return 0


# ---- watches -------------------------------------------------------------

def load_watches() -> tuple[list[dict], list[str]]:
    watches, problems, seen = [], [], set()
    if not TRIPS.is_dir():
        return watches, problems
    for page in sorted(TRIPS.rglob("*.md")):
        text = page.read_text(encoding="utf-8", errors="replace")
        for raw in BLOCK.findall(text):
            where = page.relative_to(config.VAULT).as_posix()
            try:
                w = normalise(json.loads(raw))
            except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
                problems.append(f"{where}: {type(exc).__name__}: {exc}")
                continue
            if w["id"] in seen:
                problems.append(f"{where}: duplicate id {w['id']}")
                continue
            seen.add(w["id"])
            w["page"] = where
            watches.append(w)
    return watches, problems


def normalise(w: dict) -> dict:
    out = {
        "id": re.sub(r"[^a-z0-9-]", "-", str(w["id"]).lower()).strip("-"),
        "from": [c.strip().upper() for c in _list(w["from"])],
        "to": [c.strip().upper() for c in _list(w["to"])],
        "days": flights.parse_days(w["dates"]),
        "return": w.get("return") or None,
        "stops": w.get("stops"),
        "bag": bool(w.get("bag", False)),
        "adults": max(1, int(w.get("adults", 1))),
        "target": float(w["target_usd"]) if w.get("target_usd") else None,
    }
    if not out["id"] or not out["from"] or not out["to"] or not out["days"]:
        raise ValueError("id, from, to and dates are required")
    for code in out["from"] + out["to"]:
        if not re.fullmatch(r"[A-Z]{3}", code):
            raise ValueError(f"not an IATA code: {code}")
    out["until"] = (date.fromisoformat(w["until"]) if w.get("until")
                    else date.fromisoformat(max(out["days"])))
    return out


def _list(value) -> list[str]:
    return value if isinstance(value, list) else str(value).split(",")


# ---- judging -------------------------------------------------------------

def observe(w: dict, offers: list) -> dict:
    regular = [o for o in offers if not o.hidden_city]
    hidden = [o for o in offers if o.hidden_city]
    best = (regular or offers)[0]
    obs = {"t": datetime.now(timezone.utc).isoformat(timespec="minutes"),
           "best": best.price_usd, "route": f"{best.origin}-{best.destination}",
           "date": best.date, "airlines": best.airlines, "stops": best.stops,
           "duration": best.duration_min, "flags": best.flags, "url": best.url,
           "source": best.source}
    if hidden and not w["bag"]:
        h = hidden[0]
        obs["hidden"] = {"price": h.price_usd, "route": f"{h.origin}-{h.destination}",
                         "date": h.date, "airlines": h.airlines, "url": h.url}
    return obs


def judge(w: dict, obs: dict, series: list, last_alert: dict | None) -> str:
    """Why this is a deal, or '' if it is not."""
    price = obs["best"]
    reasons = []
    if w["target"] and price <= w["target"]:
        reasons.append(f"at or under the ${w['target']:.0f} target")
    if len(series) >= 4:
        seen = [s["best"] for s in series]
        median = statistics.median(seen)
        if price <= median * 0.88 and median - price >= 40:
            reasons.append(f"{(1 - price / median) * 100:.0f}% under the usual ${median:.0f}")
        if price <= min(seen) * 0.95:
            reasons.append(f"new low (was ${min(seen):.0f})")
    if not reasons:
        return ""
    if last_alert:
        since = datetime.fromisoformat(obs["t"]) - datetime.fromisoformat(last_alert["t"])
        if price > last_alert["price"] * 0.95 and since < timedelta(days=7):
            return ""
    return ", ".join(reasons)


def push_text(w: dict, obs: dict, series: list, verdict: str) -> dict:
    day = datetime.strptime(obs["date"], "%Y-%m-%d").strftime("%b %-d")
    title = f"✈️ {obs['route']} {day}: ${obs['best']:.0f}"
    lines = [verdict[0].upper() + verdict[1:] + ".",
             f"{obs['airlines']}, {obs['stops']} stop(s), {obs['duration'] // 60}h"
             + (f", {obs['flags']}" if obs["flags"] else "") + f" ({obs['source']}).",
             ]
    h = obs.get("hidden")
    if h and h["price"] <= obs["best"] * 0.7 and obs["best"] - h["price"] >= 100:
        lines.append(f"Hidden-city ${h['price']:.0f} ({h['route']}): carry-on only, one-way.")
    lines.append("Confirm on the airline and Google Flights before booking. "
                 f"Trip page: {w['page'].rsplit('/', 1)[-1][:-3]}")
    return {"title": title, "message": "\n".join(lines), "url": obs["url"]}


def deliver(job: Job, pending: list) -> list:
    """Push what is pending unless it is quiet hours; keep the rest for later."""
    if not pending or job.dry_run:
        for p in pending:
            log(f"flight_watch: [dry-run] would push: {p['title']} | {p['message']}")
        return [] if job.dry_run else pending
    if quiet_hours.is_quiet():
        log(f"flight_watch: quiet hours, holding {len(pending)} push(es)")
        return pending
    left = []
    for p in pending:
        try:
            notify.send_push(p["title"], p["message"], url=p.get("url") or None,
                             url_title="Open the fare", priority=0)
        except notify.PushUnavailable as exc:
            try:
                notify.send_mail(f"[PUSH FAILED] {p['title']}",
                                 f"The push failed ({exc}), so this mail is the alert.\n\n"
                                 f"{p['message']}\n\n{p.get('url', '')}")
            except Exception:
                left.append(p)
    job.record("pushes sent", len(pending) - len(left))
    return left


# ---- status page ---------------------------------------------------------

def write_status_page(watches: list, history: dict, alerts: dict, job: Job) -> None:
    now = datetime.now(timezone.utc)
    healthy = not job.failures
    L = ["---", "type: reference", "component: flight-watch-status",
         f"last_run: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}", f"healthy: {str(healthy).lower()}",
         "---", "", "# Flight watch — status", "",
         f"**Generated {now.strftime('%Y-%m-%d %H:%M')} UTC by `~/automation` "
         "(`jobs/flight_watch.py`). Do not edit; every run overwrites this file.** "
         "How it works: [[travel]]. Watches are the `flight-watch` blocks in the trip pages "
         "under `Areas/Personal/Travel/`. Prices are leads from Skiplagged and Kiwi in USD, "
         "not quotes.", ""]
    if not watches:
        L += ["No watches.", ""]
    today = clock.today()   # his day, Pacific (audit A4)
    for w in watches:
        series = history.get(w["id"], [])
        page = w["page"][:-3]
        state = "expired" if w["until"] < today else "watching"
        L += [f"## {w['id']}", "",
              f"[[{page}|{page.rsplit('/', 1)[-1]}]] · {state} until {w['until']:%b %-d} · "
              f"{','.join(w['from'])} → {','.join(w['to'])} · departing "
              f"{', '.join(datetime.strptime(d, '%Y-%m-%d').strftime('%b %-d') for d in w['days'])}"
              + (f" · return {w['return']}" if w.get("return") else "")
              + (f" · {w['adults']} passengers" if w["adults"] > 1 else "")
              + (" · checked bag" if w["bag"] else " · carry-on")
              + (f" · target ${w['target']:.0f}" if w["target"] else ""), ""]
        if not series:
            L += ["Not checked yet.", ""]
            continue
        prices = [s["best"] for s in series]
        last = series[-1]
        alert = alerts.get(w["id"])
        L += [f"- **Now ${last['best']:.0f}** ({last['route']} {last['date']}, {last['airlines']}, "
              f"{last['stops']} stop(s){', ' + last['flags'] if last['flags'] else ''}) · "
              f"low ${min(prices):.0f} · median ${statistics.median(prices):.0f} · "
              f"high ${max(prices):.0f} · {len(series)} checks since {series[0]['t'][:10]}",
              f"- Last deal push: {'$%.0f on %s' % (alert['price'], alert['t'][:10]) if alert else 'none'}",
              ""]
        # One row per day: the day's lowest check. Enough for a trend, short
        # enough to read on a phone.
        by_day: dict[str, dict] = {}
        for s in series:
            d = s["t"][:10]
            if d not in by_day or s["best"] < by_day[d]["best"]:
                by_day[d] = s
        L += ["| Checked | Best USD | Pair | Departs |", "|---|---|---|---|"]
        for d in sorted(by_day)[-21:]:
            s = by_day[d]
            L.append(f"| {d} | {s['best']:.0f} | {s['route']} | {s['date']} |")
        L.append("")
    # Written in place, like maintenance-status: a temp file beside it would
    # be picked up by Obsidian Sync, which carries unsupported types on the box.
    STATUS_PAGE.write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
