# What the existing jobs taught us

Every row is a real failure or near-miss on this box, and what the runner now
does so it cannot happen again. Add to this file when something bites; a lesson
that only lives in a chat log is a lesson that gets relearned.

## Silence and false signals

**A green process says nothing about its output.** `claude-rc` printed
`Connected` for a full day while holding blanked tokens, because it never
crashed. `Restart=always` gave zero protection. Ten weeks earlier the WhatsApp
bridge answered every query from a frozen snapshot without erroring.
→ *The runner never treats "still running" as health. Every job asserts
something about its output, and the off-box ping catches a job that stops
running entirely.*

**A silent job is indistinguishable from a dead one.** `~/.fitness-sync/cron.log`
is 0 bytes dated the day it was created, because `sync.py` writes to its own
log and says nothing on stdout. It looks abandoned. It ran fine an hour ago.
→ *The runner logs an outcome line on every run, success included. No job is
allowed to complete silently.*

**Check the job's own state, not its downstream artifact.** The maintenance
registry said to check `training-log.md` was fresh within ~2h. That file only
changes when the owner actually trains, so a healthy job with a quiet week looks
identical to a broken one.
→ *`state.json` is written on every run, including runs that found nothing to
do. That is the freshness signal; output files are not.*

**Failure-only alerting makes silence ambiguous.** If nothing ever arrives, you
cannot tell healthy from dead.
→ *Section 5 of the Monday maintenance brief (until 2026-10-03, the weekly
digest mail), heartbeat's CRITICAL check that the brief ran, and the off-box
heartbeat.*

**A channel nobody reads is not a channel.** Every IMPORTANT finding mailed,
and the owner does not read those mails (2026-10-03). The mails were accurate and
reached no one.
→ *Findings go to an append-only alert log in the vault and arrive as at most
five decisions in the Monday brief. Only CRITICAL interrupts, by push. Mail is
the fallback when push or the log fails, never the primary path.*

**A flock lock cannot be left behind by a dead process.** The kernel releases
it when the holder dies, so the runner's own `locks/*.lock` files are never
stale in a way that blocks anything, however old they look. The lock a dead
process does strand is git's `index.lock`: a killed `git commit` leaves it and
every vault snapshot fails after.
→ *`runner/repairs.py` clears only git lock files, only when older than 10
minutes and no git process is running.*

**A test that misses one stub sends the real alert.** `tests/wa_tripwire`
stubbed `send_mail` only. When `send_urgent` became push-first, its simulated
trip sent a real high-priority "WhatsApp BAN TRIPWIRE" push during a full test
run (2026-10-04), indistinguishable from a real ban warning.
→ *Every test stubs every channel it can reach and points `config.ENV_FILE` at
nothing. Underneath, `runner/notify` prefixes `[TEST] ` to anything a test
process really delivers (push, mail, Telegram), so a missed stub is at least
labelled.*

## Writing things safely

**A dry run that writes state is not a dry run.** `remember()` persisted
`failed_keys` unconditionally while `_write_state()` and `_ping()` both refused
on a dry run. So rehearsing a job taught it its failures were already known, and
the next *real* run marked them `ongoing` and never mailed. Found 2026-09-02 the
only way it can be found: a new job's genuine first finding produced no note,
because the job had been dry-run three times while being built.
→ *The guard lives inside `remember()`, not at its call site, so a future caller
cannot get it wrong. State is still written on every run - it is descriptive
("did I run"). Memory is decisive ("should the next failure alert"), and a
rehearsal must never decide that. `tests/dry_run_isolation.py` holds the line.*


**Rotating credentials must be re-persisted or the chain breaks.** Strava
rotates its refresh token on every call and `sync.py` writes the new one back
to `config.json`. A run that dies mid-write breaks authentication days later,
silently.
→ *State writes are atomic: write to a temp file, then `os.replace`. A job
killed mid-write never leaves truncated JSON for the brief to choke on.*

**An atomic write with a fixed temp name is only atomic for one writer.**
`wa_consumer` wrote `wa-session-status.tmp` then `os.replace`d it. WAHA sends
`session.status` in pairs milliseconds apart, one handler thread each, so one
thread renamed the other's temp away: 16 "could not persist" warnings in a week
(2026-09-28 to 2026-10-04), and in a test, interleaved writes left corrupt JSON.
Both threads also read the same previous status, so one outage could alert twice.
→ *`_write_json_atomic()` uses `tempfile.mkstemp` in the target dir, one temp
file per call, and creates parents. The status read-compare-write holds a lock.
`tests/wa_session_status.py` races 16 writers with a slowed `os.replace`.*

**Two writers on one file have no conflict mechanism.** Obsidian Sync
serialises near-simultaneous edits into ordinary last-write-wins; it only makes
a proper conflict copy under genuine divergence. An agent editing a note while
the owner edits it on his phone loses one of them.
→ *`notify.write_note()` creates only, never edits, and returns `None` if the
file exists. The capability to edit an existing note does not exist in the
runner.*

## Fetching other people's servers

**`urllib.robotparser.read()` turns a 403 into "disallow everything", silently.** It fetches
robots.txt with the default Python User-Agent, which several publishers answer with 403. The
first Canvas link run reported 24 of 39 readings as `robots-disallowed`, including pages whose
robots.txt plainly allows them. It looked like polite behaviour while dropping 60% of the
reading list.
→ *`tools/canvas/links.py` fetches robots.txt itself, with the same UA the real request uses.*

**urllib re-sends every header across a redirect, including `Authorization`.** Canvas file URLs
redirect off-host to inst-fs, which would have handed the API token to a CDN.
→ *`tools/canvas/api.py` installs a redirect handler that strips `Authorization` on any host
change. Do not remove it.*

**A disabled tab is not an error.** Concluded courses routinely 403 on quizzes or files. A run
that unwinds on the first one archives nothing.
→ *401/403/404 raise `Unavailable`, which callers record per section and step over.*

**A cached failure that was never permanent looks like success on the retry.** `vt canvas
extract` recorded `missing:pypdf` for 26 files when the box had no virtualenv, then treated
that as a final answer. Installing the parsers and re-running printed `26 skipped` — which
reads exactly like "already done" and would have left the whole archive as embeds with no
text in it.
→ *Only `true`, `no-text-layer` and `unsupported` are terminal. `missing:*`, `error:*` and
`pending` retry on their own.*

**`.venv/` is gitignored, so it exists on whichever machine somebody made it on.** The Canvas
archive was documented as needing one and then run on a box that had none. Everything that
did not need a parser worked, which is why it took a real pull to notice.
→ *Creating it also changes the interpreter for every job, since both entry points prefer it.
Run `selftest`, `inventory`, `todos`, `profiles` and `backlinks` after, not before.*

**A listing endpoint is not the authority on what exists.** Canvas keeps course files in
three places - the Files tab, module items, and plain links inside page HTML - and a GSB
course usually 403s the first. Reading only the Files tab reported 48 files across an entire
MBA; reading all three, and following subpages, reported 975. The first number looked like a
working tool.
→ *Collect from every source and union them. Never let one listing decide the answer.*

**A destination that moves strands what it used to hold.** A layout change moved where a
binary belongs while state still recorded the old path, so the file counted as current, was
never re-downloaded, and never moved. `--prune` then removed 22 of 26 PDFs as orphans -
correctly, by its own rules.
→ *Relocate on a path change rather than trusting "current". A prune is only as safe as the
set of paths the run claims to have written, so every write path must register itself, and
that is asserted in the test rather than remembered.*

**A pass that rewrites its own input destroys it.** Rewriting reading links into wikilinks
removed the URLs from the notes the harvester reads, so the next run found 11 URLs instead
of 91 and a deleted note could never be rebuilt.
→ *State remembers every URL and where its note went; the harvest is seeded from it.*

**Comparing output against a remembered hash is not comparing it against the file.** The
pull stored a content hash, another pass edited the same files, and the next pull reported
105 notes unchanged while stale content sat on disk.
→ *Compare against the file. The hash was an optimisation that made the tool wrong about
its own output.*

## Credentials

**A pasted secret lands wherever the cursor is.** Two attempts to put a Canvas token into
`~/.automation.env` failed in two different ways: a paste landed in the command line and
overwrote the literal string `CANVAS_TOKEN`, then a paste in an editor landed on the
neighbouring line and became the value of `CANVAS_TOKEN_EXPIRES`. Both produced a file that
looked plausible and was wrong, and both burned a token.
→ *`bin/set-canvas-token` prompts with input hidden, shape-checks both values before writing,
and strips existing `CANVAS_` lines so a retry cannot leave duplicates. The command is short
enough that there is nothing to mis-paste into.*

**Never print a credential field to check it.** Reading back `CANVAS_TOKEN_EXPIRES` to confirm
a date printed a token, because the token was in that field. Check lengths, counts and shapes;
never values.

## Logs and disk

**Never redirect a unit to its own file.** `claude-rc.service` used
`StandardOutput=append:` and produced a 555 MB log of the same repainted
terminal frame, owned by root, growing forever.
→ *Every unit writes to stdout and stderr only. journald rotates. No job may
define a log file.*

## Mail

**Hetzner blocks outbound 25 and 465 by default. 587 works.** A blocked port
*hangs until timeout* rather than refusing, which looks exactly like a bad
password and wasted twenty minutes.
→ *`config.SMTP_PORT = 587`, STARTTLS, with the reason recorded there.*

**Google app passwords are shown as four groups of four.** Pasted with the
spaces they are 19 characters and Gmail rejects them with `535 5.7.8`, the same
error as a genuinely wrong password.
→ *`notify.send_mail()` strips whitespace before authenticating.*

**A well-formed app password that still fails is usually minted on the wrong
Google account.** Signed into both Stanford and personal, the app-passwords
page silently follows whichever account is active.

**Mail failing must never fail the job.** A network problem is not the job's
fault, and a non-zero exit would fire the alert path for something the alert
path also cannot deliver.
→ *Mail errors are caught and logged; the note has already landed.*

## Scheduling

**`Restart=always` does nothing for a process that does not crash.**
→ *`OnFailure=` on a `oneshot` unit, plus the off-box ping.*

**A job that only warns at startup never warns.** Claude Code prints "login
expires in 3 days" when it starts; `claude-rc` ran 28 days without restarting,
so it fired zero times.
→ *Warnings come from a timer outside the thing being watched, never from
inside it.*

**Use `Persistent=true`.** A box that was down when a job was due should run it
on next boot rather than skipping the window silently.

**Stagger with `RandomizedDelaySec`** so several jobs never contend in the same
second.

**`TimeoutStartSec=600` in the shared template bounds the whole run of a
oneshot.** A job whose model turn may take longer is killed mid-run and reads
as a crash. `build_brief` asks with a 1500 s timeout under that 600 s limit.
→ *`maintenance_brief` ships a per-instance drop-in,
`units/automation@maintenance_brief.service.d/timeout.conf`, rather than
raising the limit for every job.*

## Code distribution

**Obsidian Sync does not carry `.py` files.** It carries markdown plus image,
audio, PDF and video. Two different `check_backlinks.py` were written fifteen
days apart on two machines, neither aware of the other, and `people.py` exists
only on the Mac. The vault's own maintenance registry recorded the drift as
"Resolved" when it had only been resolved on one machine.
→ *Code lives in this git repo. Docs live in the vault. Never the reverse.*

**`systemctl` does not exist on macOS.** Running a restart without an `ssh`
prefix authenticates sudo locally and then does nothing at all. This burned
three attempts before anyone read the shell prompt.

**Nothing in this repo runs on the Mac.** `python3 -m jobs.selftest` from a Mac
shell fails with `No module named 'jobs'`, correctly: the repo is on vault-host.
Every command here is `ssh vault-host '...'` or run from a shell on the box.

**`~/.local/bin` is on the login PATH but NOT on the non-interactive PATH.**
`ssh -t vault-host renew-claude` returned `command not found` even though the
script existed and worked when run by hand, because `ssh host command` uses a
non-interactive shell that never sources the profile which adds that directory.
→ *Anything meant to be invoked as `ssh host <command>` is symlinked into
`/usr/local/bin`, which is on the default PATH. Do not rely on `~/.local/bin`
for a command an alert email tells someone to run.*

## Dependencies

**Not every job can run under the system interpreter.** fitness sync needs its
own virtualenv for the Strava and WHOOP clients. A shared template hardcoding
`/usr/bin/python3` could not run it, and a second template for "jobs with
dependencies" would be a second architecture.
→ *`bin/run-job` picks the interpreter. One template, one entry point.*

## Documentation

**A registry rots the moment something is added without updating it.** Adding a
job means adding a row to `_system/cron-jobs.md` in the same session.

**`verified:` is a date, not a boolean.** A component doc that says
`verified: 2026-06-01` is telling you to distrust it.
