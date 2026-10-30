# Telegram bridge v1: build notes

Branch `tg-bridge`, built 2026-10-03 against the vault's
`_system/telegram-bridge/Archive/proposals/build-plan.md` and `user-perspective.md`, adapted to
`docs-tg/phase0-spike-results.md`. **Live on vault-host since 2026-10-03** as `tg-bridge.service`,
merged to master 2026-10-04. The component doc is `_system/telegram-bridge.md` in the vault; this
file is the build record.

## What is built

| File | What it does |
|---|---|
| `jobs/tg_bridge/telegram.py` | Raw Bot API over httpx (lazy import). One `Bot.call()` for every request: per-token 429 limiter honouring `retry_after`, droppable calls (typing, status) skipped under a limit, retry only on `NotSent` (connect/pool errors), never on `MaybeSent` (read timeouts). HTML send with plain fallback on "can't parse entities". getUpdates poller: first call timeout 0, then 30 with a 45 s client timeout, 409 -> deleteWebhook + fresh transport + backoff, 401/404 fatal. Token redacted from every error. |
| `jobs/tg_bridge/inbox.py` | SQLite (WAL) inbound queue. Raw update committed, THEN the offset advanced (separate commit). Offset keyed `<bot id>:<sha256(token)[:16]>`. Dedupe by update_id and a partial unique index on (chat_id, message_id). States pending/adopted/done/dead. `recover()` returns pending (processed) and adopted (notice, then dead). Transcript erased on finish. |
| `jobs/tg_bridge/sessions.py` | SQLite session store: lane (chat_id, thread_id or 0) -> named sessions + current pointer; self-minted uuid4, `started`, `warm`, `aborted_last_run`, created_at, session_started_at, last_interaction_at, updated_at; message id -> session map for quote-reply routing. Every write in a `BEGIN IMMEDIATE` transaction. |
| `jobs/tg_bridge/agent.py` | `AgentPool` over `ClaudeSDKClient` (lazy import, `SdkClient` actor so connect/disconnect stay in one task). Per-session lock for the whole turn. `decide_resume()` + `check_transcript()` (user/assistant entries only, no sidechains, dangling tool_use, missing file, half-written last line) -> fresh uuid + one-line note to agent and user. "No conversation found" -> one fresh retry. Auth failure -> `AuthFailed`, no retry. Watchdog: fresh 300 s / resume 150 s before first event, 300 s between events, 900 s while a tool runs, 45 min per turn, paused while an approval waits. Warm clients: max 2 (LRU close), idle close 10 min, reconnect (resumed) when the reply-style note changes. `interrupt()` with a 10 s drain, then a forced close. Allow/deny rules, env scrub. |
| `jobs/tg_bridge/approvals.py` | can_use_tool -> message with `[Allow once] [Deny]`, callback `tga1:<8 hex>:<a\|d>` (<64 bytes), 120 s then deny, owner-only taps, verdict edited onto the message and buttons removed, deny without asking if the command is over 3,500 UTF-16 units or has control/bidi characters. `PermissionResultAllow(updated_permissions=None)` only: the CLI's suggestions are never applied (spike 3). |
| `jobs/tg_bridge/render.py` | Markdown -> Telegram HTML (bold, italic, strike, code, pre with language, safe-scheme links, bullets, headings as bold, tables as aligned `<pre>`, blockquotes). Fence-aware chunking at 4,000 UTF-16 units (paragraph, then line, then word, then hard; fences closed and reopened with their language). Over 3 chunks: lead message + the whole answer as `.md`. Plain-text renderer for the fallback. |
| `jobs/tg_bridge/media.py` | Largest photo, getFile + download (20 MB cap, refused with a message), save to `<vault>/Attachments/Telegram/<YYYY-MM>/<YYYY-MM-DD HHMMSS> <safe name>` with exclusive create. Groq `whisper-large-v3-turbo` then OpenAI (`whisper-1`, `TG_OPENAI_STT_MODEL` to change), `.oga` -> `.ogg`, 60 s, under-1 KB and Whisper-silence guards. Transcript framing, `📝 "..."` echo, untrusted-content wrapper. |
| `jobs/tg_bridge/commands.py` | `/new [text]`, `/stop`, `/status`, `/sessions [name]` (+ inline switch buttons `tgs1:<id>`), `/start`/`/help`; stop words exact after NFC, lowercase and trailing-punctuation strip: stop, stopp, aufhören, abort, halt, /stop. |
| `jobs/tg_bridge/__main__.py` | Daemon: flock single instance (`locks/tg_bridge.lock`), env via `tg_bridge.env()` (process env, then `~/.automation.env`), exit 78 on bad config, owner filter (from.id == TELEGRAM_OWNER_ID, private chat, not a bot; everything else silently dead), one `Lane` worker per (chat, topic), 300 ms debounce (500 ms albums, 1.5 s split pastes, cap 5x), collect mode with `[Queued messages while agent was busy]`, stop bypass, typing every 4 s with a 15 min cap, status line (1.5 s delay, edits at most 1/s, droppable, deleted before the answer), quote-reply to the last trigger, replies in the right `message_thread_id`, restart recovery, failure notices + `send_urgent` mail (10 min throttle), graceful SIGTERM (120 s for a running turn). |
| `runner/notify.py` | `send_telegram(text, *, thread_id=None) -> message_id` and `TelegramUnavailable`, beside `send_push`. Plain text, owner chat, token redacted. Existing functions untouched. |
| `units/tg-bridge.service` | `Type=simple`, `bin/run-job tg_bridge`, `Restart=on-failure`, `RestartSec=10`, `StartLimitBurst=5` / `StartLimitIntervalSec=600` in `[Unit]`, `RestartPreventExitStatus=78`, `KillMode=mixed`, `TimeoutStopSec=150`, `OOMPolicy=continue`, `OnFailure=automation-alert@tg_bridge.service`. Not installed. |
| `requirements.txt` | `claude-agent-sdk>=0.2.163`, `httpx>=0.28`. |
| `docs-tg/reply-style.draft.md` | Draft for `<vault>/_system/telegram-bridge/reply-style.md`. Not written to the vault. |

## Security boundary (since 2026-10-03)

- **`bypassPermissions` plus an ask list** (owner decision, 2026-10-03, vault build-plan decision 4).
  `TG_PERMISSION_MODE` defaults to it; `default` brings the taps back. Still asking, via the SDK's
  `settings` option (`{"permissions": {"ask": [...]}}`): `rm -rf`, `rm -r`, `sudo`, `git push`,
  `systemctl stop|disable`, `mcp__gws__send_gmail_message`. Verified on the box that explicit ask
  rules reach `can_use_tool` under bypass, despite the SDK's `CanUseToolShadowedWarning`.
- **The secret-file and env-dump deny rules were removed** the same day: they made the WhatsApp read
  fail silently (a blocked command never gets a button). An agent here can read every env file on
  the box. Bash ask rules are prefix matches, so `rm -fr` or `find -delete` do not ask.
- **Still denied:** `CronCreate`, `ScheduleWakeup`, `Monitor`, `Bash(run_in_background:true)`.
  `TG_DISALLOWED_EXTRA` (";"-separated) appends more.
- **Env scrub:** after reading its config, `main()` removes every secret-looking variable from
  `os.environ` (explicit list plus any name with TOKEN/SECRET/PASSWORD/API_KEY/PRIVATE_KEY), keeping
  `CLAUDE_CODE_OAUTH_TOKEN`; the SDK `env` option blanks the same keys again. This keeps the
  bridge's own keys out of the agent's environment; it does not stop the agent reading files.
- **The lock is the owner filter** (`from.id == TELEGRAM_OWNER_ID`, private chat, not a bot).
- Tested in `tests/tg_bridge/sessions_health.py` and `sdk_shapes.py`.

## How to run the tests

Repo convention (standalone scripts with `ok/FAIL` lines, exit 1 on failure), no pytest:

    python3 -m tests.tg_bridge                    # all, bare interpreter (sdk_shapes skips)
    .venv/bin/python -m tests.tg_bridge           # with claude-agent-sdk + httpx installed
    python3 -m tests.tg_bridge.serialization      # one module

Modules: `inbox_offsets`, `serialization`, `render_chunks`, `approvals_owner`, `sessions_health`,
`media_voice`, `delivery_notify`, `sdk_shapes`, `fixes_live`, `proactive_quiet`. Takes about 15 s.
`tests.maintenance_brief` covers the brief's Telegram delivery and its Pushover fallback.

## Stubbed or not built

- Not built by instruction: topic creation by the bot, webhooks, groups, pairing, TTS.
- No `bridge doctor` command, no local transcript log for reseeding a fresh session (research-1 item
  11), no idle auto-reset (deliberately: user-perspective § 7 says no automatic reset by default).
- Proactive messages (Phase 6, 2026-10-04): one caller, the Monday maintenance brief, with
  [Create task] [Skip] for items that need the owner and [Let Claude fix it] [Skip] for items Claude
  can finish alone (`jobs/tg_bridge/proactive.py`). A fix opens a topic (`createForumTopic`,
  untested live; falls back to a named session), runs the item as a session, and its OUTCOME line
  closes the task when no input from the owner was needed. Fix log:
  `~/.local/state/automation/brief-fixes.json`. Tests: `tests/tg_bridge/brief_fix.py`. Which other content moves from
  Pushover is still open (vault: whatsapp-bridge OPEN 2, push-notifications).
- `/new` does not first ask Claude to flush durable facts to the vault (research-5 C, optional).

## Guesses to verify

1. **Topics in private chats.** `inbox.topic_of()` treats any `message_thread_id` in a *private*
   chat as a topic (groups need `is_topic_message`). Unverified until the Phase 0 topics spike runs
   with a real bot. The "General" topic and whether `sendChatAction`/`editMessageText` accept the
   thread id in bot private chats are open (tdlib/telegram-bot-api#905).
2. **SDK error surfaces.** Where "No conversation found" and "not logged in" actually appear
   (exception text, stderr callback, `ResultMessage.result`/`errors`, `AssistantMessage.error`) is
   unverified; `classify()` checks all of them. The fresh-retry path is tested only against a fake
   that raises on connect.
3. **`Bash(run_in_background:true)`** as a deny rule is OpenClaw's form; whether the CLI parses it is
   unknown (harmless if not).
4. **Watchdog numbers** are simplified from OpenClaw (fresh 300 / resume 150 / between events 300 /
   tool 900 / turn 45 min). A long `vt canvas pull` could exceed the 900 s tool limit.
5. **`TYPING_MAX_S` = 15 min** and the 3,500-unit approval display limit are judgement calls.
6. **OpenAI fallback model** defaults to `whisper-1`; `gpt-4o-transcribe` is a one-variable change.
7. **`ANTHROPIC_*` scrubbed** on the assumption the box authenticates by OAuth login (spike 1 says
   so). If it ever uses an API key, add it to `agent.ENV_KEEP`.
8. **`editMessageText` without `reply_markup` removes the keyboard** (used to strip approval buttons
   with the verdict). Falls back to `editMessageReplyMarkup` if the edit fails.
9. A crash after media is saved but before the turn is adopted re-downloads and saves the file again
   on restart (a duplicate attachment, never a lost one).
10. MCP servers are left as the box configures them (parity: gws gives Gmail/Calendar), per the
    coordinator; spike 7 measured about 410 MB per warm client with gws.
