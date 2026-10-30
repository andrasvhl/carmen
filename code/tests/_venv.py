"""Re-run the importing test under ~/automation/.venv/bin/python.

    try: import tests._venv          # first import in the test
    except ModuleNotFoundError: import _venv   # run by path, not -m

The Canvas tests need the venv's packages (trafilatura above all), and the
system python3 fails them in ways that look like code bugs: five
canvas_links_state checks on both machines until 2026-10-04. Importing this
replaces the process with the same command under the venv interpreter. Already
in the venv, or no venv on this machine: does nothing.
"""
import os
import pathlib
import sys

_VENV = pathlib.Path(__file__).resolve().parents[1] / ".venv"
_PY = _VENV / "bin" / "python"

if (_PY.exists() and pathlib.Path(sys.prefix).resolve() != _VENV.resolve()
        and not os.environ.get("AUTOMATION_VENV_REEXEC")):
    # The guard stops a loop if the venv python ever reports another prefix.
    os.environ["AUTOMATION_VENV_REEXEC"] = "1"
    sys.stdout.flush()
    os.execv(str(_PY), [str(_PY), *sys.orig_argv[1:]])
