#!/usr/bin/env python3
"""A course renamed on Canvas is moved, not recreated - his notes go with it.

    ~/automation/.venv/bin/python -m tests.canvas_rename

Before 2026-09-23 a rename pulled into the new folder and then rmtree'd the old
one, deleting any note of the owner's inside it. Runs `vt move` for real, against
a scratch vault only.
"""
try: import tests._venv  # noqa: F401  re-runs under the venv
except ModuleNotFoundError: import _venv  # noqa: F401  run by path, not -m
import json, os, pathlib, sys, tempfile

TMP = pathlib.Path(tempfile.mkdtemp(prefix="canvas-rename-"))
os.environ["VAULT_PATH"] = str(TMP / "vault")   # before runner.config is imported
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tools.canvas import __main__ as M, archive
from tools.canvas.layout import course_dir

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not cond: fails.append(name)

check("scratch vault, not the real one", str(archive.ROOT).startswith(str(TMP)), str(archive.ROOT))

OLD, COURSE = "Robotics Studio Projects", {"id": 900112, "name": "Robotics Studio Projects",
                                        "course_code": "F26-CS-351Z-01"}
NEW = archive.course_folder(COURSE)
old_dir = course_dir(archive.ROOT, OLD, None)
(old_dir / "1 - Intro").mkdir(parents=True)
(old_dir / "1 - Intro" / "1 - Intro.md").write_text("tool note", encoding="utf-8")
(old_dir / "1 - Intro" / "My notes on agents.md").write_text("mine", encoding="utf-8")
(archive.FILES_ROOT / OLD / "1 - Intro").mkdir(parents=True)
(archive.FILES_ROOT / OLD / "1 - Intro" / "deck.pdf").write_bytes(b"%PDF")
elsewhere = archive.VAULT / "Areas" / "MBA" / "Agents thoughts.md"
elsewhere.parent.mkdir(parents=True, exist_ok=True)
elsewhere.write_text(f"see [[{old_dir.relative_to(archive.VAULT)}/1 - Intro/1 - Intro]]", encoding="utf-8")

data = {"courses": {"900112": {"folder": OLD, "items": {
            "page:intro": {"path": str(old_dir / "1 - Intro" / "1 - Intro.md")}},
        "files": {"1": {"path": str(archive.FILES_ROOT / OLD / "1 - Intro" / "deck.pdf"),
                        "note": f"{old_dir.relative_to(archive.VAULT)}/1 - Intro/Materials/deck.pdf.md"}}}},
        "readings": {}}

err = M._follow_rename(data, COURSE, None)
new_dir = course_dir(archive.ROOT, NEW, None)
check("no error", err is None, str(err))
check("renamed course lands in its quarter", new_dir.parent.name == "Fall 2026", str(new_dir))
check("his note moved with the course", (new_dir / "1 - Intro" / "My notes on agents.md").exists())
check("old folder is gone", not old_dir.exists())
check("binaries followed", (archive.FILES_ROOT / NEW / "1 - Intro" / "deck.pdf").exists())
check("a link from elsewhere was rewritten", NEW in elsewhere.read_text(encoding="utf-8"),
      elsewhere.read_text(encoding="utf-8"))
c = data["courses"]["900112"]
check("state folder updated", c["folder"] == NEW)
check("state note paths updated", NEW in c["items"]["page:intro"]["path"], c["items"]["page:intro"]["path"])
check("state binary paths updated", f"_files/{NEW}/" in c["files"]["1"]["path"], c["files"]["1"]["path"])

print()
print(f"{len(fails)} failure(s)" + (": " + ", ".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
