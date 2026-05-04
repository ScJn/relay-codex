# Relay Codex

Relay Codex is a Windows-oriented remote control worker for Codex. It lets an authorized chat dispatch work to a local Codex CLI/app-server session, keep per-project context, resume conversations, cancel running work, and send follow-up guidance while the agent is already working.

[中文文档](README.zh-CN.md)

The key idea is to combine remote control with a Teambition task pool. When you are away from the machine, the hard part is often not just "how do I trigger Codex remotely?", but "what should I ask it to do right now?". Telegram provides the control channel, while Teambition provides a living backlog of concrete tasks. The bot can read board tasks and surface them in status or idle prompts, so you can turn backlog items into Codex work without having to remember every detail from your phone.

## Features

- Telegram long polling with an allowlist of chat IDs.
- Local Codex task execution through `codex app-server`.
- Per-project session persistence for continued conversations.
- Commands for project selection, status, cancellation, new chat, guidance, and goal mode.
- Optional Teambition task lookup through environment-provided credentials.
- A remote workflow where Telegram is the command channel and Teambition is the task source.
- Windows startup helper script.
- Runtime logs and secrets excluded from git.

## Why Teambition + Telegram

Remote coding control has a practical bottleneck: the operator may not know what task to send next. A plain chat bot can accept commands, but it cannot provide a reliable task source by itself.

This project addresses that by connecting three pieces:

- Teambition keeps the real project backlog, task lists, priorities, and task context.
- Telegram lets you operate the local machine from anywhere.
- Codex executes selected work inside the configured local project directories.

That makes the workflow less dependent on memory. You can check the current board, pick an item, start a Codex turn, add guidance, cancel it, or continue the same project thread later.

## Requirements

- Windows with PowerShell or Command Prompt.
- Python 3.10+.
- Codex CLI available on `PATH` as `codex`, `codex.cmd`, or `codex.exe`.
- Python packages:

```powershell
pip install requests python-dotenv websockets
```

## Configuration

Copy the example environment file and fill in your own values:

```powershell
copy .env.example .env
```

Required Telegram settings:

```dotenv
TELEGRAM_BOT_TOKEN=123456789:replace_with_your_bot_token
TELEGRAM_ALLOWED_CHAT_IDS=123456789
```

Optional Telegram settings:

```dotenv
TELEGRAM_POLL_TIMEOUT=30
TELEGRAM_MAX_PARALLEL_TASKS=3
```

Optional Teambition settings:

```dotenv
TEAMBITION_APP_ACCESS_TOKEN=replace_with_your_teambition_app_access_token
TEAMBITION_APP_ID=replace_with_your_teambition_application_id
TEAMBITION_APP_SECRET=replace_with_your_teambition_application_secret
TEAMBITION_TENANT_ID=replace_with_your_teambition_org_id
TEAMBITION_TENANT_TYPE=organization
TEAMBITION_PROJECT_ID=replace_with_your_teambition_project_id
TEAMBITION_IDLE_BOARD_LIST=doing
```

If `TEAMBITION_APP_ACCESS_TOKEN` is omitted, the tool can generate an app access token from `TEAMBITION_APP_ID` and `TEAMBITION_APP_SECRET`.

`TEAMBITION_IDLE_BOARD_LIST` controls which board list the Telegram bot reads when it builds idle-work prompts. The default is `doing`, but it can be any board list name in your Teambition project, such as `todo`, `backlog`, `Ready`, or a localized list name.

## Project Mapping

The Telegram project shortcuts are currently configured in `scripts/remote_codex_bot.py`:

```python
PROJECTS = {
    "/f2": Path(...),
    "/f3": Path(...),
    "/f4": Path(...),
    "/f5": Path(...),
}
```

Change these paths before running the bot on another machine.

## Run

Validate configuration:

```powershell
python scripts\remote_codex_bot.py --check-config
```

Start the bot:

```powershell
python -u scripts\remote_codex_bot.py
```

Or use the included Windows batch file:

```powershell
.\start_remote_codex_bot.bat
```

Logs are written under `logs/` and ignored by git.

## Telegram Commands

- `/projects` - list configured project shortcuts.
- `/f2`, `/f3`, `/f4`, `/f5` - select a project.
- `/status` - show current running and pending tasks.
- `/new_chat` - reset the next task to a new Codex thread.
- `/cancel` or `/cancel <task_id>` - cancel running work.
- `/guide <task_id> <message>` - steer a running Codex turn with live guidance; if live steering fails, it is queued as follow-up guidance.
- `/goal <objective>` - start or continue work with a Codex goal.

After selecting a project, send any normal message to start a Codex task in that project.

## Teambition Helper

Teambition integration is optional, but it is the part that turns the bot from a generic remote shell into a task-aware coding assistant.

List Teambition board lists:

```powershell
python scripts\teambition_project_tasks.py --list-lists
```

Export unfinished tasks:

```powershell
python scripts\teambition_project_tasks.py --list doing --format markdown --output teambition_tasks.md
```

`doing` is only an example list name. The CLI accepts any board list name returned by `--list-lists`, and `--list-id` can be used when list names are duplicated or localized.

Generated task exports are ignored by git because they may contain private project, user, and task IDs.

The Telegram bot also uses the same Teambition client for idle-work prompts. For example, when Codex is available and the configured board has pending items, the bot can summarize those items so the next remote instruction can be based on the current backlog rather than a vague memory of what needs to be done.

## Windows Startup

`start_remote_codex_bot.bat` can be placed in the current user's Startup folder, or referenced by a shortcut there:

```text
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
```

The batch file sets UTF-8 related Python environment variables and writes timestamped logs.

## Security Notes

- Never commit `.env`.
- Never commit `logs/`.
- Rotate any token that has appeared in old logs, shell history, screenshots, or git history.
- Use `TELEGRAM_ALLOWED_CHAT_IDS` to restrict who can run local Codex tasks.
- The bot starts Codex with broad local permissions by design. Run it only on a trusted machine and only for trusted Telegram chats.

## License

This project is open source under the [MIT License](LICENSE).
