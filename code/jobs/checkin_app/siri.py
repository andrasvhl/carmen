"""Carmen through Siri (Nov 8): a spoken conversation with the phone locked.

owner decision, Nov 8: "push something on my airpods to open / trigger this voice chat ... when im running
and hands off without pulling my phone out". A web app loses the mic once the screen locks, so
the AirPods path is Siri: the "Carmen" Shortcut (tools/build_carmen_shortcut.py) loops Ask for
Input (Siri speaks the reply and listens) and POSTs each answer to /api/talk. This module is the
part of that route with rules: who may call it (a token, since Shortcuts has no login), what a
turn waits for, and what is said back. It owns no data: the conversation is a Chat tab thread
(`general:<thread>`), so it shows up in Past chats afterwards.

  CARMEN_TALK_TOKEN   in ~/.automation.env; the Shortcut sends it as x-carmen-token
  CARMEN_TALK_LIVE    "1" (default): Siri turns run live; "0": test mode, read-only
"""
from __future__ import annotations

import asyncio
import hmac

WAIT_S = 25.0            # Siri gives up on a slow request; better to say "still working" in time
POLL_S = 0.5
SPOKEN_MAX = 700         # Siri reads the prompt of Ask for Input; a long one is tiring to hear
STILL_WORKING = "Still working on it. Say anything, or ask what's new, and I'll tell you when it's done."
NOTHING = "Done. There's nothing more to say about it."

# Siri hears every reply; the agent writes for that (the Chat tab's Talk asks the same).
SIRI_NOTE = ("\n[Talk mode via Siri: the owner is running with AirPods, the phone locked. Siri reads your reply "
             "aloud and he answers by voice. Answer in one to three short spoken sentences: no lists, headings, "
             "links, tables, code or task ids. Do quick work first, then say the result. Anything that needs his "
             "tap in the app (an approval, a choice between drafts) waits until he is back: say so and move on.]")


# The iPhone app's background Talk (Nov 8, the owner: "close the phone and continue talking"): the app
# records natively, so it keeps listening with the screen locked; the server transcribes and answers.
APP_WAIT_S = 60.0        # the app waits longer than Siri does before saying "still working"
APP_NOTE = ("\n[Talk mode in the Carmen iPhone app: the owner may have the phone locked, with AirPods. Your reply is "
            "read aloud and he answers by voice. Answer in the language of his message (German or English; keep "
            "names and English terms as they are), in one or two short spoken sentences, under about 12 seconds "
            "aloud: no lists, headings, links, tables, code or task ids. Do quick work first, then say the result "
            "in one clause (\"Ist drin.\", \"Done.\"). If there is more, end with a short offer instead of reading "
            "it all. Anything that needs his tap in the app (an approval, a choice between drafts) waits until he "
            "looks: say so and move on. When he wants to end the conversation (\"you can close it now\", \"that's all\", "
            "\"tschüss\", \"beende das Gespräch\"), say a short goodbye and put [end talk] at the very end: the app "
            "then hangs up by itself.]")

# The streamed Talk (Nov 8): each text block is spoken the moment it exists, so a short first
# sentence before slow work is heard while the work runs ("Okay, ich trag den Termin ein").
STREAM_NOTE = ("\n[Your text is spoken as you write it. Before work that needs tools or takes more than a "
               "few seconds, first write one short sentence that says what you are about to do and restates "
               "the key detail (\"Okay, ich trag den Termin mit Leo morgen um drei ein.\"), then do it, then "
               "say the result in one clause. For a quick answer, just answer.]")

# A turn that ends mid-thought (Nov 8, voice UX research #6, simple version): held and joined
# with the next one instead of going to Claude half-said.
UNFINISHED = ("und", "and", "aber", "but", "oder", "or", "weil", "because", "dass", "that", "also", "so",
              "ähm", "äh", "hm", "hmm", "um", "uh", "der", "die", "das", "the", "a", "mit", "with", "für", "for")


def unfinished(text: str) -> bool:
    t = (text or "").strip()
    if not t or t.endswith(("?", "!")):
        return False
    if t.endswith(("...", "…", ",")):
        return True
    last = t.rstrip(".").split()[-1].lower().strip(",") if t.rstrip(".").split() else ""
    if last in ENDS_SENTENCE and t.endswith("."):
        # "Ja, mach das." is whole; a trailing article without a full stop is not. Nov 11: "I'm not
        # sure what you mean by that." was held as half-said, so Talk went back to listening
        # after he had finished (thread talk-20261109-1444, 14:45:33).
        return False
    return last in UNFINISHED


ARTICLES = ("der", "die", "das", "the", "a", "mit", "with", "für", "for")
ENDS_SENTENCE = ARTICLES + ("that", "so", "also", "too")   # with a full stop, a whole sentence


def unsaid(final: str, spoken: list[str]) -> str:
    """The part of a turn's saved final answer not yet spoken as a text block ("" when all of it
    was). Nov 11: the final was skipped whenever the last spoken block was a substring of it, so a
    final that repeated the "what I'll do" sentence and then gave the answer was never heard."""
    import re
    f = (final or "").strip()
    if not f or not spoken:
        return f
    rest, cut = f, False
    for s in spoken:
        s = (s or "").strip()
        if not s:
            continue
        if rest.strip() and rest.strip() in s:
            return ""
        if s in rest:
            rest, cut = rest.replace(s, " ", 1), True
    if not cut:
        return f
    rest = " ".join(rest.split()).lstrip(" .,;:-")
    return rest if re.search(r"\w{2,}", rest) else ""


# Approving by voice in Talk (Nov 8, the owner: "is it possible to approve by voice when in voice mode").
# Only a short, clear answer decides; anything longer or mixed is asked again, never guessed.
_NO = ("nein", "no", "nope", "nicht", "lieber nicht", "deny", "abbrechen", "stopp", "stop", "don't", "dont", "cancel")
_YES = ("ja", "yes", "yeah", "yep", "ok", "okay", "erlauben", "erlaub", "erlaube", "allow", "approve", "mach",
        "machs", "mach es", "go ahead", "klar", "sicher", "passt", "do it", "sure", "genau")


def voice_verdict(text: str):
    """True for a spoken yes, False for a no, None when unclear (or more than 6 words)."""
    t = " ".join((text or "").lower().replace("!", " ").replace(".", " ").replace(",", " ").replace("?", " ").split())
    words = t.split()
    if not words or len(words) > 6:
        return None
    padded = f" {t} "
    no = any(f" {w} " in padded for w in _NO)
    yes = any(f" {w} " in padded for w in _YES)
    if no == yes:
        return None
    return yes


def approval_line(ap: dict, german: bool) -> str:
    """The spoken question for a waiting approval: what it is for, then yes or no."""
    import re
    if ap.get("speak"):   # a browser step: what it does, where, after entering what (Nov 9)
        if german:
            return f"Ich brauche dein OK im Browser: {ap['speak']}. Sag ja oder nein."
        return f"I need your OK in the browser: {ap['speak']}. Say yes or no."
    what = (ap.get("why") or ap.get("verb") or "do something").strip()
    what = re.sub(r"mcp__\w+?__(\w+)", lambda m: m.group(1).replace("_", " "), what).replace("_", " ")
    shown = re.sub(r"https?://(www\.)?", "", str(ap.get("shown") or ""))
    host = re.search(r"([a-z0-9-]+\.[a-z.]{2,})", shown)
    extra = f", {host.group(1)}" if host else ""
    if german:
        return f"Ich brauche dein OK: {what}{extra}. Sag ja oder nein."
    return f"I need your OK to {what}{extra}. Say yes or no."


def looks_german(text: str) -> bool:
    import re
    w = set(re.findall(r"[a-zäöüß]+", (text or "").lower()))
    return any(c in (text or "") for c in "äöüß") or len(w & {"ich", "du", "und", "nicht", "ist", "das", "der",
                                                              "die", "ja", "nein", "bitte", "was", "wie", "mal"}) >= 1


# Claude's marker that the app should end Talk after speaking (Nov 8, the owner: "close the conversation by
# just talking to you"). Tolerant of speakable() dropping the brackets.
END_RE = __import__("re").compile(r"\s*\[?\s*end[ _-]talk\s*\]?[\s.!]*$", __import__("re").I)


def split_end(text: str) -> tuple[str, bool]:
    """(text without the end marker, whether it was there)."""
    t = text or ""
    m = END_RE.search(t)
    return (t[:m.start()].rstrip(), True) if m else (t, False)

# Whisper invents these from silence or wind (voice UX research, Nov 8): never send them to Claude.
HALLUCINATIONS = (
    "untertitel im auftrag des zdf", "untertitel der amara.org-community", "untertitelung des zdf",
    "danke fürs zuschauen", "vielen dank fürs zuschauen", "bis zum nächsten mal",
    "thank you for watching", "thanks for watching", "subtitles by the amara.org community",
    "please subscribe", "you",
)


def unheard(text: str) -> bool:
    """True when a transcript is empty or one of Whisper's known silence hallucinations."""
    t = " ".join((text or "").lower().replace("!", " ").replace(".", " ").replace(",", " ").split())
    return not t or t in HALLUCINATIONS or any(t.startswith(h) and len(t) <= len(h) + 12 for h in HALLUCINATIONS[:9])


def authorised(given: str, token: str) -> bool:
    return bool(token) and len(token) >= 16 and hmac.compare_digest(str(given or "").encode(), token.encode())


def live(env: dict) -> bool:
    return (env.get("CARMEN_TALK_LIVE") or "1").strip() != "0"


async def wait_idle(state, key: str, timeout: float | None = None, poll: float | None = None) -> dict:
    """Poll the chat until its turn is over, an approval is waiting, or the time is up."""
    timeout, poll = WAIT_S if timeout is None else timeout, POLL_S if poll is None else poll
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    st = state(key)
    while st.get("busy") and not st.get("approval") and loop.time() < end:
        await asyncio.sleep(poll)
        st = state(key)
    return st


def spoken_reply(st: dict, seen: int) -> tuple[str, int]:
    """What to say after message index `seen`: Claude's visible messages since, made speakable.
    Returns (text, new seen). A turn still running says so; an approval says what it waits on."""
    from . import speech
    msgs = st.get("messages") or []
    if st.get("approval"):
        verb = (st["approval"] or {}).get("verb") or "do something"
        return f"Carmen needs your OK in the app to {verb}. I'll leave it waiting.", seen
    new = [i for i in range(seen, len(msgs)) if msgs[i].get("role") == "claude" and not msgs[i].get("hidden")]
    if not new:
        return (STILL_WORKING if st.get("busy") else NOTHING), seen
    text = speech.speakable(speech.reply_text(st, new), limit=SPOKEN_MAX)
    if st.get("busy"):
        text = (text + " " if text else "") + "Still working on the rest."
    return text or NOTHING, len(msgs)


# ---- turns that outlive their stream (Nov 11, talk-20261111-1659: the same content four times) ----
# A tool turn ran past its stream's 60 s, he spoke again, and the next stream spoke the late
# answer's text blocks, then the late answer, then its own near-duplicate, while the app's poll
# delivered both answers once more. Now every turn has an id (chat.py), a stream speaks only its
# own turn, and a late turn's answer is spoken once: by the open stream as a framed lead-in, or by
# the poll when no stream is open. Claude is told, so it does not answer the same thing again.
LATE_QUEUED_NOTE = ("[Talk note: he spoke while your previous turn was still running. Your answer to that "
                    "turn is read to him right before this one, so do not repeat it; answer only what this "
                    "message adds, in a sentence or two.]\n\n")
LATE_TOLD_NOTE = ("\n[Talk note: your previous answer reached him late, after he had moved on, and is read "
                  "to him separately. Do not repeat it unless he asks.]")


def turn_answer(msgs: list[dict], turn) -> tuple[str, bool]:
    """(Claude's answer to that turn, whether the turn is over). A failed or stopped turn's note
    is its answer; a recap note, an unprompted result and hidden messages never are."""
    if turn is None:
        return "", False
    mine = [m for m in msgs if m.get("turn") == turn and m.get("role") in ("claude", "note")
            and not m.get("recap") and not m.get("unprompted")]
    if not mine:
        return "", False
    said = [m["text"] for m in mine if m["role"] == "claude" and not m.get("hidden") and (m.get("text") or "").strip()]
    return ("\n\n".join(said) if said else mine[-1].get("text", "")), True


def late_lead(text: str, german: bool) -> str:
    """A late answer, framed as the answer to the earlier question."""
    t = (text or "").strip()
    if not t:
        return ""
    return ("Zu deiner Frage vorhin: " if german else "About what you asked before: ") + t
