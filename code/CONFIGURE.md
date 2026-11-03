# CONFIGURE

This is a scrubbed copy of a personal automation repo. Personal values were
replaced with placeholders in angle brackets. Before running anything, search
for each one below and set your own value (or delete the line if you do not
use that feature). `grep -rn '<YOUR_' .` finds most of them.

Secrets are never in the code: they are read from `~/.automation.env`
(mode 0600) by `runner/config.py`. See `README.md` for the layout.

| Placeholder | What to set | Files |
|---|---|---|
| `<B2_KEY_NAME>` | The name you gave your Backblaze B2 application key (bin/fix-b2-keyid.sh message only). | `bin/fix-b2-keyid.sh` |
| `<COACHING_PROJECT>` | Example project/course name in comments (a video course you transcribed). | `jobs/heartbeat.py`, `jobs/inventory.py`, `tests/move_links.py`, `tools/move.py`, `tools/transcribe/__main__.py` |
| `<CONTACT>` | Example contact name in an example calendar event. | `jobs/plan_today_prerun.py` |
| `<COURSE_SITE>` | Example course website in tools/transcribe usage text. | `tools/transcribe/__main__.py` |
| `<group>` | A WhatsApp group name or substring, in tools/freetime.py usage text. | `tools/freetime.py` |
| `<GROUP_CHAT>` | Example WhatsApp group name in comments. | `runner/waha_store.py` |
| `<GROUP_NAME>` | Example WhatsApp group name in tools/freetime.py usage text. | `bin/vt`, `tools/freetime.py` |
| `<LUMA_CALENDAR_ID>` | Optional: the id of an imported Luma calendar (ends @import.calendar.google.com). jobs/plan_today_prerun.py BLOCKING_CALENDARS; delete if unused. | `jobs/plan_today_prerun.py` |
| `<ORG>` | Example organisation in tools/people.py usage text. | `tools/people.py` |
| `<PARTIFUL_CALENDAR_ID>` | Optional: the id of an imported Partiful calendar. Same place; delete if unused. | `jobs/plan_today_prerun.py` |
| `<PHONE>` | Example phone number in international format, digits only (docstring in jobs/wa_consumer.py). The real allowlist is WA_ALLOWLIST in ~/.automation.env. | `jobs/wa_consumer.py` |
| `<PRIVATE_NOTE>` | An example note name in comments. | `jobs/core_learnings.py`, `tests/core_learnings_backlog.py` |
| `<PROJECT>` | A project folder name used as an example path in comments. | `jobs/core_learnings.py`, `jobs/routine_push.py`, `tests/core_learnings_backlog.py`, `tools/move.py` |
| `<SAMPLE_VAULT_FILE_1>` | bin/restore-test.sh restores four files from your vault and compares them byte for byte against the live copy. Set _1 to _4 to real vault-relative paths (a PDF, a note with non-ASCII in its name, anything you care about). | `bin/restore-test.sh` |
| `<SAMPLE_VAULT_FILE_2>` | See <SAMPLE_VAULT_FILE_1>. | `bin/restore-test.sh` |
| `<SAMPLE_VAULT_FILE_3>` | See <SAMPLE_VAULT_FILE_1>. | `bin/restore-test.sh` |
| `<SAMPLE_VAULT_FILE_4>` | See <SAMPLE_VAULT_FILE_1>. | `bin/restore-test.sh` |
| `<user>` | Your Unix user name (home paths /home/<user>, /Users/<user>; systemd User=; launchd label com.<user>.*). Set in units/*.service and units/mac/*.plist. | `jobs/tg_bridge/agent.py`, `jobs/wa_consumer.py`, `tests/session_digest.py`, `tests/tg_bridge/_fakes.py`, `tests/tg_bridge/delivery_notify.py`, `tests/tg_bridge/sessions_health.py` and 6 more |
| `<VAULT_NAME>` | Your Obsidian vault's name (folder name, and the obsidian:// vault= parameter). Set in runner/config.py, tools/check_tasks.py, tools/check_docs.py and the OBSIDIAN_VAULT / VAULT_NAME constants. | `jobs/build_brief.py`, `jobs/checkin_app/__init__.py`, `jobs/maintenance_brief.py`, `jobs/routine_push.py`, `runner/config.py`, `tests/canvas_tasks.py` and 8 more |
| `<WORK_MAIL_DOMAIN>` | The domain of your work or school mailbox (e.g. a university). jobs/checkin_app labels calendars and mailboxes on it "Work". Leave the placeholder if you have one mailbox. | `jobs/checkin_app/__init__.py`, `jobs/checkin_app/page.html` |
| `<YOUR_EMAIL>` | Your main Google account (calendar + Gmail the jobs read). Set in tools/freetime.py ACCOUNT, jobs/plan_today_prerun.py, tools/trains.py UA. | `jobs/checkin_app/page.html`, `jobs/plan_today_prerun.py`, `tests/checkin_app_send.py`, `tools/freetime.py`, `tools/trains.py` |
| `<YOUR_PERSONAL_ACCOUNT>` | Label for that second account in comments. | `jobs/plan_today_prerun.py` |
| `<YOUR_PERSONAL_EMAIL>` | Optional second Google account whose calendar/mail is also read. Set in jobs/plan_today_prerun.py or delete the entry. | `jobs/checkin_app/__init__.py`, `jobs/checkin_app/page.html`, `jobs/plan_today_prerun.py`, `tests/checkin_app_send.py` |

Course numbers in comments and test fixtures are made up; the subject codes are real Stanford ones.

Conventions kept as-is: the server is reached as the ssh alias `vault-host`;
the vault lives at `~/vault` on the server and `~/Obsidian/<VAULT_NAME>` on a Mac.
Stanford/Canvas-specific tools (`tools/canvas/`) assume canvas.stanford.edu.

7 files of the original repo were left out because they only made
sense for the original owner or held real data.
