#!/usr/bin/env python3
"""Build the mobile capture Shortcut ("Add to Inbox") as a .shortcut file.

Replaces the Shortcuts app's AI builder, which kept typing variable names as
literal text. Spec and rationale: vault _system/apple-shortcuts-capture.md.
Usage: python3 build_capture_shortcut.py OUT_DIR   (then: shortcuts sign)
"""
import plistlib, sys, uuid

OBJ = "￼"
actions = []


def U():
    return str(uuid.uuid4()).upper()


def var(name):
    return {"Value": {"Type": "Variable", "VariableName": name},
            "WFSerializationType": "WFTextTokenAttachment"}


def out(u, name):
    return {"Value": {"Type": "ActionOutput", "OutputUUID": u, "OutputName": name},
            "WFSerializationType": "WFTextTokenAttachment"}


def tokens(*parts):
    """parts: plain strings, or ('var', name). Returns a WFTextTokenString."""
    s, att = "", {}
    for p in parts:
        if isinstance(p, tuple):
            pos = len(s.encode("utf-16-le")) // 2
            att["{%d, 1}" % pos] = {"Type": "Variable", "VariableName": p[1]}
            s += OBJ
        else:
            s += p
    return {"Value": {"string": s, "attachmentsByRange": att},
            "WFSerializationType": "WFTextTokenString"}


def add(ident, **params):
    actions.append({"WFWorkflowActionIdentifier": "is.workflow.actions." + ident,
                    "WFWorkflowActionParameters": params})
    return params


def setvar(name, value):
    add("setvariable", WFVariableName=name, WFInput=value)


def text(*parts):
    u = U()
    add("gettext", UUID=u, WFTextActionText=tokens(*parts))
    return out(u, "Text")


# 1-2. inputs
u = U(); add("ask", UUID=u, WFAskActionPrompt="Name", WFInputType="Text", WFAllowsMultilineText=False)
setvar("name", out(u, "Provided Input"))
u = U(); add("ask", UUID=u, WFAskActionPrompt="Description (optional)", WFInputType="Text", WFAllowsMultilineText=True)
setvar("description", out(u, "Provided Input"))

# 3. dates
ud = U(); add("date", UUID=ud, WFDateActionMode="Current Date")
for fmt, name in (("yyyy-MM-dd'T'HH:mm", "captured"), ("yyyy-MM-dd", "today")):
    u = U()
    add("format.date", UUID=u, WFDateFormatStyle="Custom", WFDateFormat=fmt,
        WFDate={"Value": {"string": OBJ, "attachmentsByRange": {"{0, 1}": {"Type": "ActionOutput", "OutputUUID": ud, "OutputName": "Date"}}},
                "WFSerializationType": "WFTextTokenString"})
    setvar(name, out(u, "Formatted Date"))

# 5. menu
g = U()
items = {
    "Action":       ("action",   ["priority: 1-none\n"], [""]),
    "Learning":     ("learning", ["priority: 1-none\n"], [""]),
    "Red Notebook": ("learning", ["priority: 1-none\n"], ["#coreLearning\n\n"]),
    "Urgent":       ("action",   ["urgent: true\npriority: 4-high\nscheduled: ", ("var", "today"), "\n"], [""]),
    # Last, so the four existing positions keep their muscle memory. Priority is
    # a placeholder here; the build branch below overwrites it from its own menu.
    "Build backlog": ("idea",    ["priority: 3-normal\n"], ["#build\n\n"]),
    # A tier of learning like Red Notebook, read in the weekly Best Year review
    # (2026-10-12). Appended for the same muscle-memory reason.
    "Best Year":    ("learning", ["priority: 1-none\n"], ["#bestYear\n\n"]),
}
BUILD_NUDGE = ("Build idea: what problem does it solve, what should it do, and where "
               "would you use it? A few sentences beat a title.")
add("choosefrommenu", GroupingIdentifier=g, WFControlFlowMode=0, WFMenuPrompt="Type", WFMenuItems=list(items))
for title, (ntype, urgent, tag) in items.items():
    add("choosefrommenu", GroupingIdentifier=g, WFControlFlowMode=1, WFMenuItemTitle=title)
    setvar("note_type", text(ntype))
    setvar("urgent_line", text(*urgent))
    setvar("tag_line", text(*tag))
    if title == "Build backlog":
        # The Description prompt ran before the type was known, so it could not
        # nudge. Ask again, only when it was left empty: the Monday brief plans an
        # implementation from this text, and a bare title is too little to plan from.
        c = U()
        add("conditional", GroupingIdentifier=c, WFControlFlowMode=0, WFCondition=100,
            WFInput={"Type": "Variable", "Variable": var("description")})
        add("conditional", GroupingIdentifier=c, WFControlFlowMode=1)
        u = U(); add("ask", UUID=u, WFAskActionPrompt=BUILD_NUDGE, WFInputType="Text",
                     WFAllowsMultilineText=True)
        setvar("description", out(u, "Provided Input"))
        add("conditional", GroupingIdentifier=c, WFControlFlowMode=2)
        # One tap: high if high, else normal (owner decision, 2026-09-30).
        gp = U()
        add("choosefrommenu", GroupingIdentifier=gp, WFControlFlowMode=0, WFMenuPrompt="Priority",
            WFMenuItems=["Normal", "High"])
        for label, value in (("Normal", "3-normal"), ("High", "4-high")):
            add("choosefrommenu", GroupingIdentifier=gp, WFControlFlowMode=1, WFMenuItemTitle=label)
            setvar("urgent_line", text(f"priority: {value}\n"))
        add("choosefrommenu", GroupingIdentifier=gp, WFControlFlowMode=2)
add("choosefrommenu", GroupingIdentifier=g, WFControlFlowMode=2)

# 6. body = full name + blank line + description, else name (name is never cut)
c = U()
add("conditional", GroupingIdentifier=c, WFControlFlowMode=0, WFCondition=100,
    WFInput={"Type": "Variable", "Variable": var("description")})
setvar("body", text(("var", "name"), "\n\n", ("var", "description")))
add("conditional", GroupingIdentifier=c, WFControlFlowMode=1)
setvar("body", var("name"))
add("conditional", GroupingIdentifier=c, WFControlFlowMode=2)

# 7-8. filename: sanitised, max 60 chars
u = U()
add("text.replace", UUID=u, WFInput=tokens(("var", "name")), WFReplaceTextRegularExpression=True,
    WFReplaceTextFind=r"[\\/:#^\[\]|]", WFReplaceTextReplace="-")
setvar("clean_name", out(u, "Updated Text"))
u = U(); add("text.match", UUID=u, WFMatchTextPattern="^.{0,60}", text=tokens(("var", "clean_name")))
u2 = U(); add("getitemfromlist", UUID=u2, WFItemSpecifier="First Item", WFInput=out(u, "Matches"))
setvar("filename", out(u2, "Item from List"))

# 9. note content
setvar("note_content", text(
    "---\ntype: ", ("var", "note_type"), "\ncaptured: ", ("var", "captured"),
    "\nstatus: inbox\n", ("var", "urgent_line"), "---\n", ("var", "tag_line"), ("var", "body")))

# 10. clipboard carries the content (URL stays short, see doc)
add("setclipboard", WFInput=var("note_content"))

# 11-13. short URL with clipboard=true
u = U(); add("urlencode", UUID=u, WFEncodeMode="Encode", WFInput=tokens(("var", "filename")))
setvar("encoded_filename", out(u, "URL Encoded Text"))
url = text("obsidian://new?vault=<VAULT_NAME>&file=Inbox/", ("var", "encoded_filename"), ".md&clipboard=true")
add("openurl", WFInput=url)

wf = {
    "WFWorkflowActions": actions,
    "WFWorkflowClientVersion": "2607.0.1",
    "WFWorkflowMinimumClientVersion": 900,
    "WFWorkflowMinimumClientVersionString": "900",
    "WFWorkflowIcon": {"WFWorkflowIconStartColor": 4282601983, "WFWorkflowIconGlyphNumber": 59511},
    "WFWorkflowImportQuestions": [],
    "WFWorkflowInputContentItemClasses": [],
    "WFWorkflowTypes": [],
    "WFWorkflowHasShortcutInputVariables": False,
}
outdir = sys.argv[1] if len(sys.argv) > 1 else "."
with open(f"{outdir}/Add to Inbox (unsigned).shortcut", "wb") as f:
    plistlib.dump(wf, f, fmt=plistlib.FMT_BINARY)
print(len(actions), "actions")
