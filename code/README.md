# automation

Scheduled jobs on vault-host. One runner, one way of doing things.

Docs live in the vault (`_system/`), code lives here. **Obsidian Sync does not
carry `.py` files**, which is how two different `check_backlinks.py` came to
exist on two machines without either knowing. Code moves by git; notes move by
Obsidian. Never the reverse.

## The exit-code contract

    exit 0   the job ran. It may have found problems; that is the job working.
             It reports them itself: CRITICAL by push, the rest into the
             vault's alert log.
    exit 1   the job itself broke. systemd's OnFailure= fires and the alert
             unit logs the crash, and pushes if it is the second in a row.

Those are different events with different causes. Collapsing them makes "your
sync is down" indistinguishable from "the thing that checks your sync is down".

## Three signals

| Signal | Fires when | Reaches you as |
|---|---|---|
| Problem found | a check fails | CRITICAL: push. Otherwise a block in `_system/maintenance/alert-log/<YYYY-MM>.md`, read by the Monday brief |
| Job broke | crash, hang, OOM, cannot start | a log block via `OnFailure=`; push on the second crash in a row |
| Absence | pings stop arriving | Healthchecks.io, off-box |

The third is the only one that survives the whole machine going away. Mail is
only a fallback now: when a push fails (`[PUSH FAILED]`) or the alert log
cannot be written (`[LOG FAILED]`). No job writes Inbox task notes; the
Monday maintenance brief (`jobs/maintenance_brief.py`) turns the week's log into
at most five decisions and asks whether to make tasks of them.

## Checks and records

Two concepts, and jobs that do real work need both.

- **`job.check(name, ok, detail)`** is an assertion. It can fail, and failing
  pushes (critical) or logs (the rest). *"Strava API responded."*
- **`job.record(name, value)`** is a measurement. It cannot fail. It lands in
  the state file, which the Monday brief reads. *"Activities fetched: 3."*

Jobs that only inspect things use checks alone. Without records, a job that
does real work has nothing to say for itself beyond a pass count.

## Adding a job

1. Write `jobs/<name>.py`. Use `Job` and call `job.check(...)`. Look at
   `jobs/selftest.py`; it is the entire shape.
2. Copy `units/automation@example.timer` to `automation@<name>.timer`, set
   `OnCalendar`, install it to `/etc/systemd/system/`.
3. `sudo systemctl enable --now automation@<name>.timer`
4. Declare what the job *watches* in `_system/maintenance/maintenance.md`, as a
   `<!--[slug]-->` in a table row, and pass the same slug as `covers=` on the
   check. The `status` job joins the two: a concern with no check reads as
   unwatched, a check naming no concern reads as undeclared, so neither side
   can rot quietly.

   **Do not add the job to a schedule registry.** `_system/cron-jobs.md` was
   retired 2026-08-28 for exactly that: hand-copying a machine fact drifted
   within three weeks, listing one timer while six were running. The schedule
   is generated into `maintenance-status.md` from the timers themselves.

You do not write locking, state handling, push, the alert log, log rotation
or failure alerting. Those are the runner's job, and a job that reimplements
one of them is a bug.

## Testing

    python3 -m jobs.<name> --dry-run     # runs checks, sends nothing
    python3 -m jobs.selftest --fail      # forces the OnFailure path

    VAULT_PATH=/tmp/canvas-test .venv/bin/python -m tests.canvas_stub_run

The last one runs the whole Canvas archive against a stub Canvas into a scratch
vault. There is deliberately no live-Canvas test - it would need a token, a
network and a real enrolment - and all four defects in the first version were
downstream of HTTP, where this does reach.

`--dry-run` also suppresses the heartbeat ping, so a manual run never makes a
dead job look alive.

## Layout

    runner/config.py    paths, credential loading
    runner/notify.py    push, mail, and create-only Inbox notes
    runner/job.py       the runner: locking, state, alerting, exit codes
    runner/alert_log.py the append-only vault alert log every finding goes to
    runner/alert.py     the OnFailure= target
    runner/repairs.py   the fixed menu of safe repairs the brief may attempt
    jobs/               one file per job
    units/              systemd templates

State is written to `~/.local/state/automation/<job>.json`, one file per job,
which is what the Monday brief and the status page read. Credentials are in `~/.automation.env`
at mode 0600, loaded only by `runner/config.py`.

## Rules inherited from the vault

The three gaps in `_system/maintenance/scheduled-execution.md` closed on
2026-08-28, so **a job may call Claude**, but only through `runner/ask.py`:

- `ask()` runs `claude -p` headless with a hardcoded read-only allowlist
  (Read, Glob, Grep, no Bash) and `--permission-mode dontAsk`. The model reads
  and returns text; it never writes. Never call `claude` any other way.
- **Python decides what lands**, and guards the answer first (`red_notebook`,
  `vaultmap`, `build_brief` are the patterns). Either as a new dated note, as a
  proposal beside the original (`ask.propose`), or as an append the model never
  saw the target of.
- Notes are created, never edited, with something varying in the title so the
  next occurrence is not suppressed by the first.
- Nothing writes into a file the owner might have open on his phone.

`runner/notify.py` enforces the note rules rather than trusting each job to
remember them.
