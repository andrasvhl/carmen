"""Flight prices from the shell: many airports and dates in one call.

Built 2026-10-14 for the `travel` skill and the `flight_watch` job. The skill's
first rule is door to door: every reasonable airport at both ends and the days
around the arrival day, which is 10 to 40 searches per question. Run one by one
from a chat that is slow and gets cut short; here it is one command.

    vt flights search BOS,PVD LHR,LGW 2027-03-02..2027-03-04
    vt flights search LHR,LGW JFK 2027-04-10,2027-04-11 --bag --stops 1
    vt flights search FRA LIS 2026-11-19 --return 2026-11-26 --json
    vt flights calendar BOS LHR 2027-03-04          cheapest fare per day, +-30 days

Sources: Skiplagged and Kiwi.com, through their public MCP endpoints. Both are
free and need no key, so this runs on vault-host with the Mac shut. Skiplagged
finds hidden-city fares; Kiwi finds self-transfer combinations and reports
the included baggage. Neither covers Southwest, and neither is the airline's
own price: the skill cross-checks Google Flights and the airline before
anything is booked (`_system/travel.md`).

Prices are converted to USD with the ECB reference rate of the day, so the two
sources compare. Every price printed here is a lead, not a quote.
"""

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

SKIPLAGGED = "https://mcp.skiplagged.com/mcp"
KIWI = "https://mcp.kiwi.com"
ECB = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
TIMEOUT = 90
WORKERS = 8
# Skiplagged rate-limits by calls per minute (measured 2026-10-14): one call
# at a time at ~7 a minute never drew a 429 across 25 calls; two or three in
# flight, or one every 2 s, drew 429s on a third of an 18-call grid. So it
# runs serially, at most one call every 8 s, and whatever was still refused
# gets one more pass after a cool-down. A 6 x 3 grid takes two to three
# minutes. Kiwi took eight in parallel without complaint.
_LIMITS = {SKIPLAGGED: threading.Semaphore(1), KIWI: threading.Semaphore(6)}
_GAP = {SKIPLAGGED: 8.0, KIWI: 0.0}
_last_start = {SKIPLAGGED: 0.0, KIWI: 0.0}
RETRY_WAITS = (20,)
COOL_DOWN = 60


class SourceError(Exception):
    pass


@dataclass
class Offer:
    source: str
    origin: str
    destination: str
    date: str
    price_usd: float
    price: str                       # as the source quoted it, e.g. "501 EUR"
    depart: str = ""
    arrive: str = ""
    duration_min: int = 0
    stops: int = 0
    airlines: str = ""
    route: list[str] = field(default_factory=list)
    hidden_city: bool = False        # ticketed beyond where you get off: carry-on only
    self_transfer: bool = False      # separate tickets: re-check bags, no missed-connection cover
    checked_bags: int | None = None  # included checked bags, when the source says
    url: str = ""
    return_date: str = ""

    @property
    def flags(self) -> str:
        out = []
        if self.hidden_city:
            out.append("HIDDEN-CITY")
        if self.self_transfer:
            out.append("self-transfer")
        if self.checked_bags == 0:
            out.append("no bag")
        elif self.checked_bags:
            out.append(f"{self.checked_bags} bag")
        return ", ".join(out)


# ---- transport -----------------------------------------------------------

def _mcp(url: str, tool: str, arguments: dict) -> dict:
    """One stateless MCP tools/call over HTTP. Both servers answer without a
    session, so there is no initialize round trip."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "vt-flights/1.0"})
    for attempt in range(len(RETRY_WAITS) + 1):
        try:
            with _LIMITS.setdefault(url, threading.Semaphore(4)):
                wait = _last_start.get(url, 0.0) + _GAP.get(url, 0.0) - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                _last_start[url] = time.monotonic()
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 502, 503) and attempt < len(RETRY_WAITS):
                time.sleep(RETRY_WAITS[attempt])
                continue
            raise SourceError(f"HTTP {exc.code}")
        except (urllib.error.URLError, OSError) as exc:
            raise SourceError(f"{type(exc).__name__}: {exc}")
    # Server-sent events: the payload is the JSON on a `data:` line.
    for line in raw.splitlines():
        line = line[5:].strip() if line.startswith("data:") else line.strip()
        if line.startswith("{"):
            msg = json.loads(line)
            if "error" in msg:
                raise SourceError(str(msg["error"])[:300])
            result = msg.get("result", {})
            if result.get("isError"):
                text = " ".join(c.get("text", "") for c in result.get("content", []))
                raise SourceError(text[:300])
            return result
    raise SourceError(f"no JSON in reply: {raw[:200]}")


_FX: dict[str, float] = {}


def usd(amount: float, currency: str) -> float:
    """Convert to USD at the ECB reference rate. ECB quotes per EUR."""
    currency = (currency or "USD").upper()
    if currency == "USD":
        return float(amount)
    if not _FX:
        try:
            with urllib.request.urlopen(ECB, timeout=20) as resp:
                tree = ET.fromstring(resp.read())
            for node in tree.iter():
                if node.get("currency") and node.get("rate"):
                    _FX[node.get("currency")] = float(node.get("rate"))
            _FX["EUR"] = 1.0
        except (urllib.error.URLError, OSError, ET.ParseError):
            _FX.update({"EUR": 1.0, "USD": 1.16})  # fallback, labelled in the output
            _FX["_fallback"] = 1.0
    if currency not in _FX or "USD" not in _FX:
        raise SourceError(f"no exchange rate for {currency}")
    return round(float(amount) / _FX[currency] * _FX["USD"], 2)


# ---- sources -------------------------------------------------------------

def skiplagged(origin: str, dest: str, day: str, *, return_date: str | None = None,
               hidden: bool = True, max_stops: int | None = None, limit: int = 40,
               adults: int = 1) -> list[Offer]:
    args = {"origin": origin, "destination": dest, "departureDate": day,
            "limit": limit, "sort": "price", "includeHiddenCity": hidden, "adults": adults}
    if return_date:
        args["returnDate"] = return_date
    if max_stops is not None:
        args["maxStops"] = {0: "none", 1: "one"}.get(max_stops, "many")
    result = _mcp(SKIPLAGGED, "sk_flights_search", args)
    offers = []
    for f in (result.get("structuredContent") or {}).get("flights", []):
        attrs = set(f.get("attributes") or [])
        price = f.get("price") or {}
        offers.append(Offer(
            source="skiplagged", origin=f.get("departure", {}).get("airport", origin),
            destination=f.get("arrival", {}).get("airport", dest), date=day,
            return_date=return_date or "",
            price_usd=usd(price.get("amount", 0), price.get("currency", "USD")),
            price=f"{price.get('amount')} {price.get('currency', 'USD')}",
            depart=f.get("departure", {}).get("dateTime", ""),
            arrive=f.get("arrival", {}).get("dateTime", ""),
            duration_min=_minutes(f.get("duration", "")),
            stops=int(f.get("layovers") or 0), airlines=f.get("airlines", ""),
            hidden_city=bool(attrs & {"hidden-city", "hiddenCity", "hidden_city", "skiplag"})
                        or "hidden" in " ".join(attrs),
            self_transfer=bool(attrs & {"virtual-interlining", "virtualInterlining",
                                        "self-transfer"}),
            url=f.get("deepLink", "")))
    return offers


def kiwi(origin: str, dest: str, day: str, *, return_date: str | None = None,
         max_stops: int | None = None, flex_days: int = 0, adults: int = 1) -> list[Offer]:
    args = {"flyFrom": origin, "flyTo": dest, "departureDate": _dmy(day),
            "currency": "USD", "departureDateFlexDays": flex_days, "adults": adults}
    if return_date:
        args["returnDate"] = _dmy(return_date)
    if max_stops is not None:
        args["max_sector_stopovers"] = max_stops
    result = _mcp(KIWI, "search-flight", args)
    payload = result.get("structuredContent")
    if not payload:
        text = "".join(c.get("text", "") for c in result.get("content", []))
        payload = json.loads(text) if text.strip().startswith("{") else {}
    currency = payload.get("currency", "USD")
    offers = []
    for it in payload.get("itineraries", []):
        out = it.get("outbound") or {}
        segs = out.get("segments") or []
        carriers = []
        for s in segs:
            name = s.get("carrierName") or s.get("carrier", "")
            if name and name not in carriers:
                carriers.append(name)
        offers.append(Offer(
            source="kiwi", origin=out.get("from", origin), destination=out.get("to", dest),
            date=(out.get("departureTime") or day)[:10], return_date=return_date or "",
            price_usd=usd(it.get("price", 0), currency),
            price=it.get("priceFormatted") or f"{it.get('price')} {currency}",
            depart=out.get("departureTime", ""), arrive=out.get("arrivalTime", ""),
            duration_min=int((out.get("durationSeconds") or 0) / 60),
            stops=int(out.get("stops") or 0), airlines=", ".join(carriers),
            route=out.get("route") or [],
            # Kiwi's own combinations mix airlines on separate tickets. It also
            # sells protected interline trips, which the API does not tell
            # apart, so mixed carriers read as self-transfer: the cautious side.
            self_transfer=len({s.get("carrier") for s in segs}) > 1,
            checked_bags=(it.get("baggage") or {}).get("checkedBag"),
            url=it.get("bookingUrl", "")))
    return offers


# ---- the grid ------------------------------------------------------------

def search_grid(origins: list[str], dests: list[str], days: list[str], *,
                return_date: str | None = None, hidden: bool = True,
                max_stops: int | None = None, adults: int = 1) -> tuple[list[Offer], list[str]]:
    """Every origin x destination x date on both sources, in parallel.
    Returns (offers, errors). A failed source is reported, never dropped.
    Prices are totals for all `adults`, as both sources quote them."""
    jobs = []
    for o in origins:
        for d in dests:
            if o == d:
                continue
            for day in days:
                jobs.append(("skiplagged", o, d, day))
                jobs.append(("kiwi", o, d, day))

    def run(job):
        src, o, d, day = job
        try:
            if src == "skiplagged":
                return skiplagged(o, d, day, return_date=return_date, hidden=hidden,
                                  max_stops=max_stops, adults=adults), None
            return kiwi(o, d, day, return_date=return_date, max_stops=max_stops,
                        adults=adults), None
        except SourceError as exc:
            return [], f"{src} {o}-{d} {day}: {exc}"
        except Exception as exc:  # a malformed reply must not sink the other 30 searches
            return [], f"{src} {o}-{d} {day}: {type(exc).__name__}: {exc}"

    offers, errors, refused = [], [], []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for job, (found, err) in zip(jobs, pool.map(run, jobs)):
            offers.extend(found)
            if err and "HTTP 429" in err:
                refused.append(job)
            elif err:
                errors.append(err)
    if refused:
        time.sleep(COOL_DOWN)
        for job in refused:
            found, err = run(job)
            offers.extend(found)
            if err:
                errors.append(err)
    if not hidden:
        offers = [x for x in offers if not x.hidden_city]
    seen, unique = set(), []
    for x in offers:
        key = (x.source, x.origin, x.destination, x.depart, x.arrive, x.price_usd)
        if key not in seen:
            seen.add(key)
            unique.append(x)
    unique.sort(key=lambda x: (x.price_usd, x.duration_min))
    return unique, errors


def calendar(origin: str, dest: str, day: str, return_date: str | None = None) -> list[dict]:
    """Skiplagged's flex calendar: cheapest one-way (or same-length round trip)
    per departure day, about 30 days either side of `day`."""
    tool = "sk_flex_return_calendar" if return_date else "sk_flex_departure_calendar"
    args = {"origin": origin, "destination": dest, "departureDate": day, "sort": "date"}
    if return_date:
        args["returnDate"] = return_date
    result = _mcp(SKIPLAGGED, tool, args)
    sc = result.get("structuredContent") or {}
    rows = []
    # The structured chart's shape is not documented; the markdown table is.
    text = "".join(c.get("text", "") for c in result.get("content", []))
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 3 and cells[0][:2] == "20":
            price = cells[2].replace("$", "").replace(",", "")
            try:
                rows.append({"depart": cells[0], "return": cells[1] if cells[1] != "—" else "",
                             "price_usd": float(price)})
            except ValueError:
                continue
    if not rows and sc:
        raise SourceError("calendar reply had no table")
    return rows


# ---- helpers -------------------------------------------------------------

def _dmy(iso: str) -> str:
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m/%Y")


def _minutes(text: str) -> int:
    h = m = 0
    for part in text.split():
        if part.endswith("h"):
            h = int(part[:-1] or 0)
        elif part.endswith("m"):
            m = int(part[:-1] or 0)
    return h * 60 + m


def parse_days(spec: str) -> list[str]:
    """'2026-11-19', '2026-11-18,2026-11-19' or '2026-11-17..2026-11-19'."""
    out = []
    for part in spec.split(","):
        part = part.strip()
        if ".." in part:
            a, b = (date.fromisoformat(x) for x in part.split(".."))
            while a <= b:
                out.append(a.isoformat())
                a += timedelta(days=1)
        elif part:
            out.append(date.fromisoformat(part).isoformat())
    return out


def _hm(minutes: int) -> str:
    return f"{minutes // 60}h{minutes % 60:02d}" if minutes else "?"


def _clock(stamp: str) -> str:
    return stamp[5:16].replace("T", " ") if stamp else ""


# ---- CLI -----------------------------------------------------------------

def _print_search(offers: list[Offer], errors: list[str], top: int, bag: bool) -> None:
    if _FX.get("_fallback"):
        print("! ECB rate unavailable: EUR converted at a fixed 1.16, prices approximate")
    if bag:
        print("Checked bag needed: hidden-city fares dropped; 'no bag' fares need a bag fee added.\n")
    print(f"{'#':>2}  {'USD':>6}  {'route':<11} {'date':<10}  {'depart':<11} {'arrive':<11} "
          f"{'time':>6} {'st':>2}  {'airlines':<28} {'flags':<22} src")
    for i, x in enumerate(offers[:top], 1):
        print(f"{i:>2}  {x.price_usd:>6.0f}  {x.origin + '-' + x.destination:<11} {x.date:<10}  "
              f"{_clock(x.depart):<11} {_clock(x.arrive):<11} {_hm(x.duration_min):>6} "
              f"{x.stops:>2}  {x.airlines[:28]:<28} {x.flags[:22]:<22} {x.source}")

    # The two questions a single search never answers: which airport pair, and
    # which day. Cheapest standard fare (no hidden-city) per cell.
    pairs, days = {}, {}
    for x in offers:
        if x.hidden_city:
            continue
        k = f"{x.origin}-{x.destination}"
        pairs[k] = min(pairs.get(k, 1e9), x.price_usd)
        days[x.date] = min(days.get(x.date, 1e9), x.price_usd)
    if len(pairs) > 1:
        print("\nCheapest regular fare per airport pair:")
        for k, v in sorted(pairs.items(), key=lambda kv: kv[1]):
            print(f"  {k:<11} {v:>6.0f}")
    if len(days) > 1:
        print("\nCheapest regular fare per day:")
        for k, v in sorted(days.items()):
            print(f"  {k}  {v:>6.0f}")
    hc = [x for x in offers if x.hidden_city]
    if hc:
        best = hc[0]
        print(f"\nCheapest hidden-city: {best.price_usd:.0f} USD {best.origin}-{best.destination} "
              f"{best.date} ({best.airlines}). Carry-on only, one-way only, no M&M number.")
    if errors:
        print(f"\n{len(errors)} searches failed:")
        for e in errors[:12]:
            print(f"  {e}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vt flights", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="every origin x destination x date, both sources")
    s.add_argument("origins", help="comma-separated IATA codes")
    s.add_argument("destinations", help="comma-separated IATA codes")
    s.add_argument("dates", help="YYYY-MM-DD, comma list, or A..B range")
    s.add_argument("--return", dest="return_date", help="return date for round trips")
    s.add_argument("--stops", type=int, choices=[0, 1, 2], help="max stops")
    s.add_argument("--adults", type=int, default=1, help="passengers; prices are totals")
    s.add_argument("--bag", action="store_true", help="needs a checked bag: drop hidden-city")
    s.add_argument("--no-hidden", action="store_true", help="drop hidden-city fares")
    s.add_argument("--top", type=int, default=25)
    s.add_argument("--json", action="store_true")

    c = sub.add_parser("calendar", help="cheapest fare per day around a date (Skiplagged)")
    c.add_argument("origin")
    c.add_argument("destination")
    c.add_argument("date")
    c.add_argument("--return", dest="return_date")
    c.add_argument("--json", action="store_true")

    a = p.parse_args(argv)
    if a.cmd == "search":
        origins = [x.strip().upper() for x in a.origins.split(",") if x.strip()]
        dests = [x.strip().upper() for x in a.destinations.split(",") if x.strip()]
        days = parse_days(a.dates)
        n = len(origins) * len(dests) * len(days)
        if n > 60:
            print(f"vt flights: {n} combinations is too many; narrow airports or dates", file=sys.stderr)
            return 2
        offers, errors = search_grid(origins, dests, days, return_date=a.return_date,
                                     hidden=not (a.bag or a.no_hidden), max_stops=a.stops,
                                     adults=a.adults)
        if a.adults > 1 and not a.json:
            print(f"Prices are totals for {a.adults} passengers.\n")
        if a.json:
            print(json.dumps({"offers": [asdict(x) | {"flags": x.flags} for x in offers[:a.top * 4]],
                              "errors": errors}, indent=1))
        else:
            _print_search(offers, errors, a.top, a.bag)
        return 0 if offers or not errors else 1

    rows = calendar(a.origin.upper(), a.destination.upper(), a.date, a.return_date)
    if a.json:
        print(json.dumps(rows, indent=1))
    else:
        low = min((r["price_usd"] for r in rows), default=0)
        for r in rows:
            mark = "  <- lowest" if r["price_usd"] == low else ""
            mark = "  <- asked" if r["depart"] == a.date and not mark else mark
            print(f"{r['depart']}  {r['return'] or '':<10}  {r['price_usd']:>7.0f}{mark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
