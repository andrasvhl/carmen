#!/usr/bin/env python3
"""Markdown -> Telegram HTML, chunking, the plain fallback, and the one 429 handler.

    python3 -m tests.tg_bridge.render_chunks

  1. **No chunk ends inside a code fence**; it is closed and reopened with its
     language, so every chunk renders on its own.
  2. **Length is UTF-16 code units**, as Telegram counts. An emoji is two.
  3. **Text is escaped, links only for safe schemes.** One stray `<` makes
     Telegram reject the whole message.
  4. **A rejected HTML message is resent as plain text**, never dropped.
  5. **Past three chunks it is a .md document**, not six messages.
  6. **429**: final sends wait `retry_after` and retry; typing and status are
     dropped. **A read timeout is never retried** (it may have been sent); a
     connect error is. The token never appears in an error.
  7. **The poller** starts with timeout 0 and answers a 409 by deleting the
     webhook and opening a fresh socket.
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from tests.tg_bridge._fakes import TOKEN, Checks, FakeTransport, fast_sleep
from jobs.tg_bridge import render
from jobs.tg_bridge.render import chunk_markdown, plan, to_html, to_plain, utf16_len, visible
from jobs.tg_bridge.telegram import Bot, MaybeSent, NotSent, Poller, TelegramError


def fences_balanced(chunk: str) -> bool:
    count = sum(1 for l in chunk.split("\n") if l.strip().startswith("```"))
    return count % 2 == 0


def run() -> list[str]:
    check = Checks("render: chunks, escaping, fallback, rate limits")

    code = "```python\n" + "\n".join(f"x_{i} = {i}  # line" for i in range(900)) + "\n```"
    md = "Intro paragraph.\n\n" + code + "\n\nOutro."
    chunks = chunk_markdown(md)
    check("a long fenced block splits into several chunks", len(chunks) > 1, len(chunks))
    check("every chunk is within 4000 UTF-16 units", all(utf16_len(c) <= 4000 for c in chunks),
          [utf16_len(c) for c in chunks])
    check("every chunk has balanced fences", all(fences_balanced(c) for c in chunks))
    check("continuation chunks reopen the fence with its language",
          all(c.lstrip().startswith("```python") for c in chunks[1:-1]))
    check("nothing is lost across the split",
          sum(c.count("# line") for c in chunks) == 900)

    emoji = "\n\n".join("🙂" * 300 for _ in range(10))      # 3000 code points, 6000 UTF-16
    e_chunks = chunk_markdown(emoji)
    check("emoji count double: 3000 code points need two chunks", len(e_chunks) == 2,
          [utf16_len(c) for c in e_chunks])
    check("no emoji chunk exceeds the UTF-16 limit", all(utf16_len(c) <= 4000 for c in e_chunks))
    long_line = "🙂" * 5000
    l_chunks = chunk_markdown(long_line)
    check("a single huge line is hard-split without breaking a surrogate pair",
          "".join(l_chunks) == long_line and all(utf16_len(c) <= 4000 for c in l_chunks))
    para = "\n\n".join(f"Paragraph {i}. " + "word " * 150 for i in range(12))
    p_chunks = chunk_markdown(para)
    check("prose splits at paragraph boundaries",
          all(c.rstrip().endswith("word") for c in p_chunks), [c[-20:] for c in p_chunks])

    html = to_html("**Bold** <script>alert(1)</script> & `a < b` [ok](https://x.y/?a=1&b=2) "
                   "[bad](javascript:alert(1))")
    check("angle brackets and ampersands are escaped",
          "&lt;script&gt;" in html and "&amp;" in html and "<script>" not in html, html)
    check("bold renders", "<b>Bold</b>" in html)
    check("inline code is escaped inside <code>", "<code>a &lt; b</code>" in html)
    check("https links become anchors with an escaped href",
          '<a href="https://x.y/?a=1&amp;b=2">ok</a>' in html, html)
    check("a javascript: link is not linked", 'href="javascript' not in html)
    h = to_html("# Heading\n- one\n- two\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```js\nx<1\n```")
    check("headings become bold", "<b>Heading</b>" in h)
    check("lists become bullets", "• one" in h and "• two" in h)
    check("tables become pre", "<pre>a  b\n1  2</pre>" in h, h)
    check("fenced code keeps its language", '<pre><code class="language-js">x&lt;1</code></pre>' in h)
    check("the plain fallback has no markup", "**" not in to_plain("**b** `c`") and
          to_plain("**b** `c`") == "b c")
    check("snake_case is not italicised", "<i>" not in to_html("my_var_name and file_name"))

    big = "\n\n".join("Paragraph " + "lorem ipsum " * 300 for _ in range(6))
    p = plan(big)
    check("more than three chunks becomes a document", p.document is not None and len(p.parts) == 1)
    check("with a short lead message", utf16_len(visible(p.parts[0].html)) < 1000)
    small = plan("Short answer.")
    check("a short answer is one part, no document", len(small.parts) == 1 and not small.document)

    async def wire():
        # Fallback: HTML refused -> same text, plain.
        t = FakeTransport()
        bot = Bot(TOKEN, t, sleep=fast_sleep)
        t.script["sendMessage"] = [{"ok": False, "error_code": 400,
                                    "description": "Bad Request: can't parse entities: x"}]
        await bot.send_rendered(1, "<b>broken", "broken", reply_to=9)
        sends = t.sent()
        check("a parse error triggers exactly one resend", len(sends) == 2, len(sends))
        check("the resend is plain text with no parse_mode",
              len(sends) == 2 and "parse_mode" not in sends[1] and sends[1]["text"] == "broken")
        check("the resend still quote-replies", len(sends) == 2 and sends[1].get("reply_parameters"))

        # 429: a final send waits and retries; a droppable one is dropped.
        t = FakeTransport()
        bot = Bot(TOKEN, t, sleep=fast_sleep, clock=lambda: 100.0)
        t.script["sendMessage"] = [{"ok": False, "error_code": 429, "description": "Too Many",
                                    "parameters": {"retry_after": 7}}]
        fast_sleep.calls.clear()
        await bot.send_message(1, "final answer")
        check("a 429'd final send is retried", len(t.sent()) == 2)
        check("after waiting retry_after", 7 in fast_sleep.calls, fast_sleep.calls)
        before = len(t.calls)
        await bot.send_chat_action(1, "typing")
        check("typing during a rate limit is dropped, not queued", len(t.calls) == before
              and bot.dropped == 1)

        # Connect error: retried. Read timeout: not.
        t = FakeTransport()
        bot = Bot(TOKEN, t, sleep=fast_sleep)
        t.script["sendMessage"] = [NotSent("sendMessage", None, "ConnectError")]
        await bot.send_message(1, "x")
        check("a send that never left is retried", len(t.sent()) == 2)
        t.script["sendMessage"] = [MaybeSent("sendMessage", None, "ReadTimeout")]
        n = len(t.sent())
        try:
            await bot.send_message(1, "y")
            raised = False
        except MaybeSent:
            raised = True
        check("a send that may have arrived is NOT retried", raised and len(t.sent()) == n + 1)

        t.script["sendMessage"] = [{"ok": False, "error_code": 400,
                                    "description": f"bad url bot{TOKEN}/x"}]
        try:
            await bot.send_message(1, "z")
        except TelegramError as exc:
            check("the token is redacted from Telegram errors", TOKEN not in str(exc), str(exc))

        # The poller: first call timeout 0, 409 -> deleteWebhook + fresh socket.
        t = FakeTransport()
        fresh = []
        bot = Bot(TOKEN, t, sleep=fast_sleep, transport_factory=lambda: fresh.append(1) or t)
        stop = asyncio.Event()
        batches = []

        async def on_batch(updates):
            batches.append(updates)
            stop.set()

        t.script["getUpdates"] = [{"ok": False, "error_code": 409, "description": "Conflict"},
                                  {"ok": True, "result": [{"update_id": 1}]}]
        await Poller(bot, lambda: None, on_batch, sleep=fast_sleep).run(stop)
        polls = [d for m, d, _ in t.calls if m == "getUpdates"]
        check("the first poll uses timeout 0", polls[0]["timeout"] == 0)
        check("a 409 deletes the webhook", any(m == "deleteWebhook" for m, _, _ in t.calls))
        check("and recreates the transport", fresh == [1])
        check("polling resumes and delivers the batch", batches == [[{"update_id": 1}]])

    asyncio.run(wire())
    return check.fails


if __name__ == "__main__":
    fails = run()
    print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'all passed'}")
    sys.exit(1 if fails else 0)
