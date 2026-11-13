#!/usr/bin/env python3
"""Pull people from the vault CRM by circle, open loop, staleness or org.

Reads FRONTMATTER ONLY. This is the point: at 200 people a full read is wasteful and
noisy, so every query here costs one cheap pass over YAML headers and never loads a
note body. Bodies are read by opening the specific person, after this narrows it.

  people.py                          # everyone, grouped by circle
  people.py --circle inner close       # the people who change his week
  people.py --owed i-owe-them        # open loops he is blocking
  people.py --cold 60                # no contact in 60+ days
  people.py --org <ORG>           # by organisation
  people.py --find hans@example.com  # resolve an email/phone/handle/alias to a person
  people.py --json                   # machine-readable
"""
import argparse, json, os, re, sys
from datetime import date, datetime

# Resolved by the shared config, not from this file's own location. These
# scripts used to live inside the vault and walk three directories up;
# they now live in ~/automation, so that trick no longer works.
from runner.config import VAULT as _VAULT
VAULT = str(_VAULT)
PEOPLE = os.path.join(VAULT, "Areas", "People")
CIRCLES = ["inner", "close", "warm", "active", "dormant"]
SKIP = {"People", "_Person Template"}


def parse_list(v):
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1]
    return [x.strip().strip('"').strip("'") for x in v.split(",") if x.strip()]


def load():
    out = []
    if not os.path.isdir(PEOPLE):
        return out
    for fn in sorted(os.listdir(PEOPLE)):
        if not fn.endswith(".md") or fn[:-3] in SKIP:
            continue
        path = os.path.join(PEOPLE, fn)
        with open(path, encoding="utf-8") as fh:
            if fh.readline().strip() != "---":
                continue
            fm = []
            for line in fh:                      # frontmatter only, then stop
                if line.strip() == "---":
                    break
                fm.append(line)
        d = dict(re.findall(r"^(\w+):[ \t]*(.*)$", "".join(fm), re.M))
        if d.get("type") != "person":
            continue
        d["name"] = fn[:-3]
        # CRM fields are prefixed crm_ since 2026-09-27; strip it so the rest of the tool
        # reads plain names. `aliases` is Obsidian's own property and has no prefix.
        d = {(k[4:] if k.startswith("crm_") else k): v.strip().strip('"') for k, v in d.items()}
        for k in ("email", "phone", "handles", "languages", "aliases"):
            d[k] = parse_list(d.get(k, ""))
        out.append(d)
    return out


def days_since(s):
    try:
        return (date.today() - datetime.strptime(s.strip(), "%Y-%m-%d").date()).days
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--circle", nargs="+", choices=CIRCLES)
    ap.add_argument("--owed", choices=["i-owe-them", "they-owe-me", "none"])
    ap.add_argument("--cold", type=int, metavar="DAYS")
    ap.add_argument("--org")
    ap.add_argument("--find", metavar="IDENTIFIER", help="resolve an email, phone or handle")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    ppl = load()
    if a.find:
        q = a.find.strip().lower().replace(" ", "")
        ppl = [p for p in ppl if q == p["name"].lower().replace(" ", "")
               or any(q == str(v).lower().replace(" ", "")
                      for k in ("email", "phone", "handles", "aliases") for v in p[k])]
    if a.circle:
        ppl = [p for p in ppl if p.get("circle") in a.circle]
    if a.owed:
        ppl = [p for p in ppl if p.get("owed") == a.owed]
    if a.org:
        ppl = [p for p in ppl if a.org.lower() in p.get("org", "").lower()]
    if a.cold is not None:
        ppl = [p for p in ppl if (days_since(p.get("last_contact", "")) or 10**6) >= a.cold]

    if a.json:
        print(json.dumps(ppl, ensure_ascii=False, indent=2))
        return
    if not ppl:
        print("No matches.")
        return

    ppl.sort(key=lambda p: (CIRCLES.index(p.get("circle", "dormant")) if p.get("circle") in CIRCLES else 9,
                            p["name"]))
    cur = None
    for p in ppl:
        if p.get("circle") != cur:
            cur = p.get("circle")
            print(f"\n{(cur or 'no circle').upper()}")
        d = days_since(p.get("last_contact", ""))
        bits = [p.get("relation", "")]
        if p.get("org"):
            bits.append(p["org"])
        if p.get("owed") == "i-owe-them":
            bits.append("→ I OWE")
        elif p.get("owed") == "they-owe-me":
            bits.append("← they owe")
        if d is not None:
            bits.append(f"{d}d ago")
        elif not p.get("last_contact"):
            bits.append("no contact date")
        print(f"  {p['name']:<22} {' · '.join(x for x in bits if x)}")
    print(f"\n{len(ppl)} person(s).")


if __name__ == "__main__":
    main()
