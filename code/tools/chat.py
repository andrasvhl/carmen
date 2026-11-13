"""LinkedIn / Messenger / Instagram from the command line, through Beeper (runner/beeper.py).

The two-minute page's Send button calls this over ssh (chatsender.py in the plan-today
skill), after his click. The text arrives on stdin, never in argv, so no quoting reaches a
shell and the approved text is sent byte for byte.

    python3 -m tools.chat held <chat_id> <since ISO>   -> JSON: messages from others since then
    python3 -m tools.chat name <chat_id>               -> JSON: {"chat", "network"}
    python3 -m tools.chat send <chat_id>  < text       -> JSON: {"ok": true} or {"ok": false, "error"}
"""

import json
import sys
from datetime import datetime

from runner import beeper


def main(argv: list[str]) -> int:
    cmd, chat_id = argv[0], argv[1]
    try:
        if cmd == "held":
            since = datetime.fromisoformat(argv[2])
            out = beeper.since_from_others(chat_id, since)
        elif cmd == "name":
            c = beeper.run("chats", "show", "--chat", chat_id)
            out = {"chat": beeper.get(c, "title", chat_id),
                   "network": beeper.label(beeper.network(c, {}))}
        elif cmd == "send":
            text = sys.stdin.read()
            if not text.strip():
                raise beeper.BeeperFailed("empty message")
            beeper.send_text(chat_id, text)
            out = {"ok": True}
        else:
            raise SystemExit(__doc__)
    except beeper.BeeperFailed as exc:
        out = {"ok": False, "error": str(exc)[:300]}
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
