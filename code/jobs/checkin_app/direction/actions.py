"""Direction, its writes: Not now on the Raise priority offer. Priority itself is written into the task
note by Tasks (apply) until Planning v2.
"""
from __future__ import annotations

import datetime as dt

from starlette.responses import JSONResponse

from . import read as direction
from ..adapters import state
from ..adapters.notes import task_path
from ..adapters.taplog import signal
from ..tasks import actions as task_actions
from ..tasks import read as tasks


def dismiss_raise(task: str, today: dt.date) -> None:
    state.update(direction.NORAISE, lambda d: d.__setitem__(task, today.isoformat()), {})


def on_raise(t):
    """raise, noraise."""
    action, row_id, dry, day_name, base_day, row = t.action, t.row_id, t.dry, t.day_name, t.base_day, t.row
    # The offer after 3 meditation mentions (Oct 31): one step up, or not now for a week.
    p = task_path(row_id)
    if not p:
        return JSONResponse({"error": "no task note"}, status_code=400)
    if action == "noraise":
        signal("raise_dismissed", row, day_name, test=dry, mentions=row.get("mind", 0))
        if not dry:
            dismiss_raise(row_id, base_day)
        return JSONResponse({"ok": True, "keep": True, "msg": "Not now"})
    cur = tasks.read_note(p).get("priority") or "1-none"
    nxt = direction.PRIO_STEPS[min(direction.PRIO_STEPS.index(cur) + 1, len(direction.PRIO_STEPS) - 1)] if cur in direction.PRIO_STEPS else "3-normal"
    signal("priority_raised", row, day_name, test=dry, mentions=row.get("mind", 0), old=cur, new=nxt)
    msg = task_actions.apply(p, "update", base=base_day, dry=dry, fields={"priority": nxt})
    return JSONResponse({"ok": True, "keep": True, "msg": "Priority raised" if msg == "Saved" else msg})
