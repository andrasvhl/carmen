"""`vt browser`: open, reuse and close the agent's Kernel browser. Doc: _system/agent-browser.md.

    vt browser use speechify      this conversation's browser, loaded with the speechify profile
    vt browser use --temp         a browser with no saved state (nothing is written back)
    vt browser status             which profile, and the live-view link to watch or take over
    vt browser close              end it now; this is what saves the profile's logins
    vt browser login speechify    for the owner: a browser on that profile, 15 min, live-view link
    vt browser done speechify     end the login browser, saving the login into the profile
    vt browser profiles           every profile on the Kernel account
    vt browser mcp                the MCP server the bridge starts (proxy.py); not for hand use

`use`, `status` and `close` act on the conversation in $BROWSER_SESSION, which the bridge sets.
The live-view link carries a token that controls the browser: send it to the owner only.
"""
from __future__ import annotations

import argparse
import os
import sys

from . import kernel as K
from . import proxy
from . import state as S


def _key() -> str:
    return S.safe_key(os.environ.get(S.ENV_KEY, ""))


def _print_state(st: dict, verb: str) -> None:
    prof = st.get("profile") or "(none, nothing saved)"
    print(f"{verb}: Kernel browser {st.get('session_id')} with profile {prof}")
    if st.get("live_view_url"):
        print(f"Live view (send to the owner only, it controls the browser): {st['live_view_url']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vt browser")
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("use")
    u.add_argument("site", nargs="?")
    u.add_argument("--temp", action="store_true")
    sub.add_parser("status")
    sub.add_parser("close")
    lg = sub.add_parser("login")
    lg.add_argument("site")
    dn = sub.add_parser("done")
    dn.add_argument("site")
    sub.add_parser("profiles")
    sub.add_parser("mcp")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "mcp":
            key = _key()
            # The key file is read lazily, so a missing key still lets the server start and
            # list its tools; the first browser call then says what is missing.
            def browsers():
                return S.Browsers(K.from_key_file())
            return proxy.run(key, lambda k: browsers().ensure(k),
                             lambda k: S.load(k).get("session_id") and browsers().close(k))

        if args.cmd == "status":
            st = S.load(_key())
            if not st:
                print("No browser open in this conversation.")
            else:
                _print_state(st, "Open")
            return 0
        b = S.Browsers(K.from_key_file())
        if args.cmd == "use":
            if not args.site and not args.temp:
                ap.error("name a site (`vt browser use speechify`) or pass --temp")
            st = b.use(_key(), None if args.temp else args.site)
            _print_state(st, "Reusing" if st.get("reused") else "Opened")
            print("Now drive it with the mcp__browser__ tools; `vt browser close` when done.")
        elif args.cmd == "close":
            st = b.close(_key())
            print("Closed" + (f"; profile {st['profile']} saved." if st.get("profile") else ".")
                  if st else "No browser was open.")
        elif args.cmd == "login":
            st = b.use(S.LOGIN_PREFIX + K.profile_name(args.site), args.site, login=True)
            _print_state(st, "Login browser")
            print("Sign in in the live view, then `vt browser done " + args.site + "` (or just "
                  "close the tab: Kernel saves the profile when the browser times out, 15 min).")
        elif args.cmd == "done":
            st = b.close(S.LOGIN_PREFIX + K.profile_name(args.site))
            print(f"Saved profile {st['profile']}." if st else "No login browser was open.")
        elif args.cmd == "profiles":
            for p in b.api.list_profiles():
                print(f"{p.get('name')}\tlast used {p.get('last_used_at') or 'never'}")
    except (K.KernelError, S.InUse, ValueError) as exc:
        print(f"vt browser: {proxy.redact(str(exc))}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
