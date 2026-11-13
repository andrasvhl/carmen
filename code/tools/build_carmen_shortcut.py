#!/usr/bin/env python3
"""Build the "Carmen" Siri Shortcut as a .shortcut file (Nov 8).

"Hey Siri, Carmen" (or hold an AirPods stem and say "Carmen") starts a spoken conversation with
Carmen with the phone locked: Siri says the prompt and listens (Ask for Input), the answer goes to
vault-host's /api/talk, and the reply becomes the next prompt. "Cancel" ends it; so does silence.
The conversation is a Chat tab thread named siri-<date-time>, readable in Carmen afterwards.
Server side: jobs/checkin_app/siri.py. Doc: vault _system/checkin-app.md § Carmen through Siri.

Generated, never built by hand or by the Shortcuts AI, like tools/build_capture_shortcut.py.
The token is read from the environment and written into the file, so the signed file is a secret:
keep it out of the vault and out of git.

Usage (on the Mac):
  CARMEN_TALK_URL=https://<your-host>/checkin/api/talk CARMEN_TALK_TOKEN=... python3 build_carmen_shortcut.py OUT_DIR
  shortcuts sign --mode anyone --input "OUT_DIR/Carmen (unsigned).shortcut" --output ~/Downloads/Carmen.shortcut
"""
import os
import plistlib
import sys
import uuid

GREETING = "Hi, it's Carmen. What's up?"
ROUNDS = 40          # Shortcuts has no while loop; 40 turns is a long run
OBJ = "￼"
actions = []


def U():
    return str(uuid.uuid4()).upper()


def out(u, name):
    return {"Value": {"Type": "ActionOutput", "OutputUUID": u, "OutputName": name},
            "WFSerializationType": "WFTextTokenAttachment"}


def tokens(*parts):
    """parts: plain strings, ('var', name) or ('out', uuid, name). A WFTextTokenString."""
    s, att = "", {}
    for p in parts:
        if isinstance(p, tuple):
            pos = len(s.encode("utf-16-le")) // 2
            att["{%d, 1}" % pos] = ({"Type": "Variable", "VariableName": p[1]} if p[0] == "var"
                                    else {"Type": "ActionOutput", "OutputUUID": p[1], "OutputName": p[2]})
            s += OBJ
        else:
            s += p
    return {"Value": {"string": s, "attachmentsByRange": att}, "WFSerializationType": "WFTextTokenString"}


def fields(pairs):
    """A dictionary field (JSON body, headers): [(key, value-token-string), ...]."""
    return {"Value": {"WFDictionaryFieldValueItems": [
                {"WFItemType": 0, "WFKey": tokens(k), "WFValue": v} for k, v in pairs]},
            "WFSerializationType": "WFDictionaryFieldValue"}


def add(ident, **params):
    actions.append({"WFWorkflowActionIdentifier": "is.workflow.actions." + ident,
                    "WFWorkflowActionParameters": params})


def setvar(name, value):
    add("setvariable", WFVariableName=name, WFInput=value)


def text(*parts):
    u = U()
    add("gettext", UUID=u, WFTextActionText=tokens(*parts))
    return out(u, "Text")


def build(token: str, url: str) -> dict:
    actions.clear()
    # 1. the thread: one per run, siri-<yyyyMMdd-HHmm>
    ud = U(); add("date", UUID=ud, WFDateActionMode="Current Date")
    uf = U()
    add("format.date", UUID=uf, WFDateFormatStyle="Custom", WFDateFormat="yyyyMMdd-HHmm",
        WFDate=tokens(("out", ud, "Date")))
    setvar("thread", text("siri-", ("out", uf, "Formatted Date")))
    setvar("prompt", text(GREETING))
    # 2. the loop: Siri says the prompt and listens; the answer goes to Carmen; her reply is next
    g = U()
    add("repeat.count", GroupingIdentifier=g, WFControlFlowMode=0, WFRepeatCount=ROUNDS)
    ua = U()
    add("ask", UUID=ua, WFAskActionPrompt=tokens(("var", "prompt")), WFInputType="Text", WFAllowsMultilineText=False)
    setvar("said", out(ua, "Provided Input"))
    uu = U()
    add("downloadurl", UUID=uu, WFURL=url, WFHTTPMethod="POST", WFHTTPBodyType="JSON",
        WFJSONValues=fields([("thread", tokens(("var", "thread"))), ("text", tokens(("var", "said")))]),
        ShowHeaders=True, WFHTTPHeaders=fields([("x-carmen-token", tokens(token))]))
    uv = U()
    add("getvalueforkey", UUID=uv, WFDictionaryKey="reply", WFInput=out(uu, "Contents of URL"))
    setvar("prompt", out(uv, "Dictionary Value"))
    add("repeat.count", GroupingIdentifier=g, WFControlFlowMode=2)
    add("speaktext", WFText=tokens("That's a long run. Start me again any time."), WFSpeakTextWait=True)
    return {
        "WFWorkflowActions": list(actions),
        "WFWorkflowClientVersion": "2607.0.1",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": 4282601983, "WFWorkflowIconGlyphNumber": 59511},
        "WFWorkflowImportQuestions": [],
        "WFWorkflowInputContentItemClasses": [],
        "WFWorkflowTypes": [],
        "WFWorkflowHasShortcutInputVariables": False,
    }


if __name__ == "__main__":
    token = os.environ.get("CARMEN_TALK_TOKEN", "").strip()
    if len(token) < 16:
        sys.exit("CARMEN_TALK_TOKEN is missing (read it from vault-host's ~/.automation.env)")
    url = os.environ.get("CARMEN_TALK_URL", "").strip()
    if not url.startswith("https://"):
        sys.exit("CARMEN_TALK_URL is missing: the app's /checkin/api/talk address, https")
    outdir = sys.argv[1] if len(sys.argv) > 1 else "."
    wf = build(token, url)
    with open(f"{outdir}/Carmen (unsigned).shortcut", "wb") as f:
        plistlib.dump(wf, f, fmt=plistlib.FMT_BINARY)
    print(len(wf["WFWorkflowActions"]), "actions")
