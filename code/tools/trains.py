"""Train and bus connections across Europe from the shell, for the travel skill.

Built 2026-10-14. The plan was borski/travel-hacking-toolkit's Deutsche Bahn
script (db-vendo-client, bahn.de's internal API). It was dropped the same day:
bahn.de's Akamai bot protection answers it with OPS_BLOCKED from both the Mac
and vault-host, its maintainers call DB access "very unreliable", and getting
around bot detection is not something this repo does.

This uses Transitous instead (api.transitous.org): a community-run, open
MOTIS router over public timetables (DB, CFL, SNCF, SNCB, NS, ÖBB, SBB, Flix
and city transit). No key; be polite, a few calls a minute.

    vt trains stations "Paris"
    vt trains journeys "Paris Gare du Nord" "Lille-Flandres" --date 2027-03-15 --time 08:00
    vt trains journeys "Chantilly" "Lille-Europe" --date 2027-03-15 --time 14:00 --arrive
    vt trains journeys be-sncb_8200100 de-DELFI_de:06412:11511 --date 2026-11-20 --json

Times are typed and printed in each stop's own local time.

Known limits:
- No fares. Read the fare on bahn.de (BahnCard 50 set in the search),
  sncf-connect.com, sncb or mobiliteit.lu.
- Timetables reach only as far as operators have published them: in Oct 2026
  that was the Dec 13 timetable change. A later date returns nothing; say so
  rather than guess.
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

API = "https://api.transitous.org/api"
UA = "vt-trains (<YOUR_EMAIL>)"
DEFAULT_TZ = "Europe/Berlin"   # central Europe; pass --tz for anywhere else


class TransitError(Exception):
    pass


def _get(path: str, params: dict) -> object:
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise TransitError(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}")
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise TransitError(f"{type(exc).__name__}: {exc}")


def stations(query: str, limit: int = 8) -> list[dict]:
    rows = _get("/v1/geocode", {"text": query, "type": "STOP"})
    return [{"id": r.get("id"), "name": r.get("name"), "lat": r.get("lat"), "lon": r.get("lon")}
            for r in rows[:limit]]


def place(text: str) -> tuple[str, str]:
    """A stop id or 'lat,lon' passes through; anything else is geocoded, a
    station first and an address or town second (a small village may have no station)."""
    if "_" in text or (text.count(",") == 1 and text.replace(",", "").replace(".", "").replace("-", "").isdigit()):
        return text, text
    for kind in ("STOP", None):
        params = {"text": text} | ({"type": kind} if kind else {})
        rows = _get("/v1/geocode", params)
        if rows:
            r = rows[0]
            ref = r["id"] if kind == "STOP" else f"{r['lat']},{r['lon']}"
            return ref, r.get("name", text)
    raise TransitError(f"no place found for {text!r}")


def journeys(origin: str, dest: str, day: str, at: str, *, arrive: bool = False,
             results: int = 5, tz: str = DEFAULT_TZ) -> dict:
    o_ref, o_name = place(origin)
    d_ref, d_name = place(dest)
    local = datetime.fromisoformat(f"{day}T{at}").replace(tzinfo=ZoneInfo(tz))
    data = _get("/v5/plan", {
        "fromPlace": o_ref, "toPlace": d_ref,
        "time": local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "arriveBy": "true" if arrive else "false", "numItineraries": results})
    out = []
    for it in data.get("itineraries", []):
        legs = []
        for leg in it.get("legs", []):
            if leg.get("mode") == "WALK":
                continue
            legs.append({
                "line": leg.get("displayName") or leg.get("routeShortName") or leg.get("mode"),
                "mode": leg.get("mode"), "operator": leg.get("agencyName", ""),
                "from": leg["from"].get("name"), "to": leg["to"].get("name"),
                "depart": _local(leg["from"].get("departure") or leg.get("startTime"), leg["from"].get("tz")),
                "arrive": _local(leg["to"].get("arrival") or leg.get("endTime"), leg["to"].get("tz")),
                "cancelled": bool(leg.get("cancelled")),
                "fare_url": leg.get("agencyFareUrl") or "",
            })
        out.append({
            "depart": _local(it["startTime"], (it["legs"][0]["from"].get("tz") or tz)),
            "arrive": _local(it["endTime"], (it["legs"][-1]["to"].get("tz") or tz)),
            "minutes": int(it.get("duration", 0) // 60),
            "changes": int(it.get("transfers", max(0, len(legs) - 1))),
            "legs": legs,
        })
    return {"from": o_name, "to": d_name, "date": day, "time": at, "arrive_by": arrive,
            "journeys": out}


def _local(stamp: str | None, tz: str | None) -> str:
    if not stamp:
        return ""
    t = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return t.astimezone(ZoneInfo(tz or DEFAULT_TZ)).strftime("%Y-%m-%d %H:%M")


def _hm(minutes: int) -> str:
    return f"{minutes // 60}h{minutes % 60:02d}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vt trains", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("stations")
    s.add_argument("query")
    j = sub.add_parser("journeys")
    j.add_argument("origin")
    j.add_argument("destination")
    j.add_argument("--date", required=True)
    j.add_argument("--time", default="08:00")
    j.add_argument("--arrive", action="store_true", help="--time is the latest arrival")
    j.add_argument("--results", type=int, default=5)
    j.add_argument("--tz", default=DEFAULT_TZ, help="time zone the --time is in")
    j.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    try:
        if a.cmd == "stations":
            for r in stations(a.query):
                print(f"  {r['id']:<48} {r['name']}")
            return 0
        res = journeys(a.origin, a.destination, a.date, a.time, arrive=a.arrive,
                       results=a.results, tz=a.tz)
    except TransitError as exc:
        print(f"vt trains: {exc}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(res, indent=1, ensure_ascii=False))
        return 0
    when = "arrive by" if a.arrive else "from"
    print(f"{res['from']} → {res['to']}, {a.date} {when} {a.time} (Transitous; no fares)\n")
    if not res["journeys"]:
        print("No connections. Either none run, or that date is past the published timetables "
              "(Dec 13, 2026 timetable change as of Oct 2026). Check bahn.de.")
        return 1
    for i, r in enumerate(res["journeys"], 1):
        print(f"{i}. {r['depart'][11:]} → {r['arrive'][11:]}  {_hm(r['minutes'])}  "
              f"{r['changes']} change(s)" + (f"  arrives {r['arrive'][:10]}" if r['arrive'][:10] != r['depart'][:10] else ""))
        for leg in r["legs"]:
            flag = "  CANCELLED" if leg["cancelled"] else ""
            print(f"     {leg['depart'][11:]}→{leg['arrive'][11:]}  {leg['line'][:10]:<10} "
                  f"{leg['operator'][:12]:<12} {leg['from']} → {leg['to']}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
