"""Ferry sailings and fares from Ferryhopper, for the travel skill.

Built 2026-10-14 with `vt flights` and `vt trains`. Ferryhopper publishes a
free MCP endpoint (https://mcp.ferryhopper.com/mcp) with no key; this calls it
over plain HTTP like tools/flights.py does for Skiplagged and Kiwi, so it
works from any session and on vault-host. Coverage: about 33 countries,
strongest for Greece, Italy, Croatia, Spain and Scandinavia.

    vt ferries search Split Hvar 2027-06-12
    vt ferries search Split Hvar 2027-06-12 --return 2027-06-16 --tz Europe/Zagreb
    vt ferries routes Split                     direct connections from a port
    vt ferries disruptions GR 2026-11-20        strikes, weather, delays

Prices are Ferryhopper's expected fares in EUR per person: the cheapest deck
or seat, and the cheapest cabin. Confirm on ferryhopper.com before booking.
"""

import argparse
import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from tools.flights import SourceError, _mcp

URL = "https://mcp.ferryhopper.com/mcp"


def search(origin: str, dest: str, day: str, return_date: str | None = None) -> list[dict]:
    legs = [{"departureLocation": origin, "arrivalLocation": dest, "departureDate": day}]
    if return_date:
        legs.append({"departureLocation": dest, "arrivalLocation": origin,
                     "departureDate": return_date})
    result = _mcp(URL, "search_ferry_trips", {"legs": legs})
    out = []
    for leg in (result.get("structuredContent") or {}).get("legs", []):
        options = []
        for opt in leg.get("options", []):
            segs = opt.get("segments", [])
            prices = [(a.get("type", ""), a.get("expectedPrice", {}).get("totalPriceInCents"))
                      for s in segs for a in s.get("accommodations", [])]
            prices = [(t, c / 100) for t, c in prices if c]
            seats = [p for t, p in prices if not t.startswith(("CABIN", "PET"))]
            cabins = [p for t, p in prices if t.startswith("CABIN")]
            options.append({
                "id": opt.get("id"),
                "depart": segs[0].get("departureDateTime") if segs else "",
                "arrive": segs[-1].get("arrivalDateTime") if segs else "",
                "operators": sorted({s.get("ownerCompany", {}).get("name", "") for s in segs}),
                "vessels": [s.get("vessel", {}).get("name", "") for s in segs],
                "changes": max(0, len(segs) - 1),
                "seat_eur": min(seats) if seats else None,
                "cabin_eur": min(cabins) if cabins else None,
            })
        out.append({"from": leg.get("departurePort", {}).get("name"),
                    "to": leg.get("arrivalPort", {}).get("name"),
                    "date": leg.get("departureDate"), "options": options})
    return out


def _local(stamp: str, tz: str) -> str:
    if not stamp:
        return "?"
    t = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return t.astimezone(ZoneInfo(tz)).strftime("%m-%d %H:%M")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vt ferries", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search")
    s.add_argument("origin")
    s.add_argument("destination")
    s.add_argument("date")
    s.add_argument("--return", dest="return_date")
    s.add_argument("--tz", default="UTC", help="print times in this zone, e.g. Europe/Athens")
    s.add_argument("--json", action="store_true")
    r = sub.add_parser("routes")
    r.add_argument("port")
    d = sub.add_parser("disruptions")
    d.add_argument("country", help="ISO code, e.g. GR")
    d.add_argument("date")
    a = p.parse_args(argv)
    try:
        if a.cmd == "routes":
            res = _mcp(URL, "get_direct_connections_for_ports", {"portLocation": a.port})
            print("".join(c.get("text", "") for c in res.get("content", []))
                  or json.dumps(res.get("structuredContent"), indent=1, ensure_ascii=False))
            return 0
        if a.cmd == "disruptions":
            res = _mcp(URL, "get_disruptions", {"tripDate": a.date, "country": a.country.upper()})
            print("".join(c.get("text", "") for c in res.get("content", []))
                  or json.dumps(res.get("structuredContent"), indent=1, ensure_ascii=False))
            return 0
        legs = search(a.origin, a.destination, a.date, a.return_date)
    except SourceError as exc:
        print(f"vt ferries: {exc}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(legs, indent=1, ensure_ascii=False))
        return 0
    for leg in legs:
        print(f"{leg['from']} → {leg['to']}, {leg['date']} (times {a.tz}, EUR per person)")
        if not leg["options"]:
            print("  no sailings")
        for o in sorted(leg["options"], key=lambda o: o["seat_eur"] or 1e9):
            seat = f"€{o['seat_eur']:.0f}" if o["seat_eur"] else "—"
            cabin = f"cabin €{o['cabin_eur']:.0f}" if o["cabin_eur"] else ""
            print(f"  {_local(o['depart'], a.tz)} → {_local(o['arrive'], a.tz)}  {seat:>5}  "
                  f"{cabin:<11} {', '.join(o['operators'])[:30]}  {o['changes']} change(s)")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
