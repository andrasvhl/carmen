#!/usr/bin/env python3
"""The Canvas lock: re-entrant in one process, refused across two.

    ~/automation/.venv/bin/python -m tests.canvas_lock

canvas.json has no lock of its own and every command rewrites it whole, so a
second writer silently discards the first one's save. The daily canvas_tasks
job made that a certainty rather than a hazard (2026-09-23).
"""
try: import tests._venv  # noqa: F401  re-runs under the venv
except ModuleNotFoundError: import _venv  # noqa: F401  run by path, not -m
import os, pathlib, subprocess, sys, tempfile

TMP = pathlib.Path(tempfile.mkdtemp(prefix="canvas-lock-"))
os.environ["HOME"] = str(TMP / "home")
os.environ["VAULT_PATH"] = str(TMP / "vault")
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.canvas.lock import Busy, canvas_lock, LOCK_FILE

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not cond: fails.append(name)

check("lock lives in the scratch HOME", str(LOCK_FILE).startswith(str(TMP)), str(LOCK_FILE))

with canvas_lock("outer"):
    try:
        with canvas_lock("inner"):
            nested = True
    except Busy:
        nested = False
    check("re-entrant in the same process (refresh calls main again)", nested)

    env = dict(os.environ)
    probe = ("from tools.canvas.lock import Busy, canvas_lock\n"
             "try:\n    with canvas_lock('probe'): print('GOT')\n"
             "except Busy as e: print('BUSY', e)\n")
    out = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, env=env,
                         capture_output=True, text=True).stdout
    check("a second process is refused", out.startswith("BUSY"), out)
    check("the refusal names the holder", "outer" in out, out)

    r = subprocess.run([sys.executable, "-m", "tools.canvas", "extract"], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    check("a writing vt canvas command refuses while held", r.returncode == 2,
          f"rc={r.returncode} {r.stderr.strip()[:120]}")
    r = subprocess.run([sys.executable, "-m", "tools.canvas", "links", "--gated"], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    check("a read-only command still runs while held", r.returncode == 0,
          f"rc={r.returncode} {r.stderr.strip()[:120]}")

out = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, env=dict(os.environ),
                     capture_output=True, text=True).stdout
check("released afterwards", out.startswith("GOT"), out)

print()
print(f"{len(fails)} failure(s)" + (": " + ", ".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
