# Relay Codex

Relay Codex 是一个偏 Windows 使用场景的 Codex 远程控制 worker。它允许授权过的聊天通道向本机 Codex CLI/app-server 下发任务，并支持按项目保存上下文、继续会话、取消任务、追加引导，以及用 goal 模式启动任务。

[English README](README.md)

这个项目的核心不是单纯“用 Telegram 远程发命令”，而是把远程控制和 Teambition 任务池结合起来。人在外面时，真正的痛点通常不是“怎么远程启动 Codex”，而是“我现在该让它做什么”。Telegram 负责随时下发指令，Teambition 负责保存真实待办、看板、优先级和任务上下文。这样你不用靠记忆临时编任务，可以直接从当前看板里挑工作交给 Codex。

## 功能

- Telegram long polling，并通过 chat id 白名单限制访问。
- 通过 `codex app-server` 在本机执行 Codex 任务。
- 按项目保存 Codex thread，方便持续上下文。
- 支持项目选择、状态查看、取消任务、新会话、追加引导和 goal 模式。
- 可选 Teambition 任务读取，凭证全部来自环境变量。
- Telegram 作为远程控制通道，Teambition 作为任务来源。
- 提供 Windows 开机启动脚本。
- 默认忽略运行日志、`.env`、状态文件和导出文件，避免误提交隐私信息。

## 为什么要结合 Teambition 和 Telegram

远程控制编码代理时有一个很实际的问题：你可能人在手机旁边，但不知道该下发什么具体任务。普通聊天机器人只能接受命令，却不能稳定提供任务来源。

这个项目把三件事连起来：

- Teambition 保存真实项目 backlog、看板列表、优先级和任务上下文。
- Telegram 让你在任何地方操作本机。
- Codex 在配置好的本地项目目录里执行实际修改。

这样工作流就不依赖临时记忆。你可以远程查看当前看板，挑一个任务，启动 Codex，继续补充指导，必要时取消，之后还可以回到同一个项目上下文继续做。

## 环境要求

- Windows，PowerShell 或 Command Prompt。
- Python 3.10+。
- Codex CLI 已加入 `PATH`，命令名为 `codex`、`codex.cmd` 或 `codex.exe`。
- Python 依赖：

```powershell
pip install requests python-dotenv websockets
```

## 配置

复制示例环境文件：

```powershell
copy .env.example .env
```

Telegram 必填配置：

```dotenv
TELEGRAM_BOT_TOKEN=123456789:replace_with_your_bot_token
TELEGRAM_ALLOWED_CHAT_IDS=123456789
```

Telegram 可选配置：

```dotenv
TELEGRAM_POLL_TIMEOUT=30
TELEGRAM_MAX_PARALLEL_TASKS=3
```

Teambition 可选配置：

```dotenv
TEAMBITION_APP_ACCESS_TOKEN=replace_with_your_teambition_app_access_token
TEAMBITION_APP_ID=replace_with_your_teambition_application_id
TEAMBITION_APP_SECRET=replace_with_your_teambition_application_secret
TEAMBITION_TENANT_ID=replace_with_your_teambition_org_id
TEAMBITION_TENANT_TYPE=organization
TEAMBITION_PROJECT_ID=replace_with_your_teambition_project_id
TEAMBITION_IDLE_BOARD_LIST=doing
```

如果没有提供 `TEAMBITION_APP_ACCESS_TOKEN`，工具可以用 `TEAMBITION_APP_ID` 和 `TEAMBITION_APP_SECRET` 生成 app access token。

`TEAMBITION_IDLE_BOARD_LIST` 控制 Telegram bot 在空闲派活提示里读取哪个看板列表。默认是 `doing`，但可以改成当前 Teambition 项目里的任意列表名，比如 `todo`、`backlog`、`Ready`，或者中文列表名。

## 项目映射

Telegram 的项目快捷命令目前配置在 `scripts/remote_codex_bot.py`：

```python
PROJECTS = {
    "/f2": Path(...),
    "/f3": Path(...),
    "/f4": Path(...),
    "/f5": Path(...),
}
```

在其他机器上运行前，需要改成自己的项目路径。

## 运行

检查配置：

```powershell
python scripts\remote_codex_bot.py --check-config
```

启动 bot：

```powershell
python -u scripts\remote_codex_bot.py
```

也可以使用内置 Windows bat：

```powershell
.\start_remote_codex_bot.bat
```

日志会写到 `logs/`，该目录默认被 git 忽略。

## Telegram 命令

- `/projects` - 查看已配置的项目快捷命令。
- `/f2`、`/f3`、`/f4`、`/f5` - 选择项目。
- `/status` - 查看当前运行中和排队任务。
- `/new_chat` - 下一次任务使用新的 Codex thread。
- `/cancel` 或 `/cancel <task_id>` - 取消运行中的任务。
- `/guide <task_id> <message>` - 给运行中的任务追加后续引导。
- `/goal <objective>` - 用 Codex goal 模式启动或继续任务。

选择项目后，发送普通消息就会在该项目里启动一次 Codex 任务。

## Teambition 辅助工具

Teambition 集成是可选的，但它让这个 bot 不只是远程命令入口，而是能感知任务池的编码助手。

列出 Teambition 看板列表：

```powershell
python scripts\teambition_project_tasks.py --list-lists
```

导出未完成任务：

```powershell
python scripts\teambition_project_tasks.py --list doing --format markdown --output teambition_tasks.md
```

这里的 `doing` 只是示例列表名。CLI 可以接收 `--list-lists` 返回的任意看板列表名；如果列表重名或本地化名称不方便，也可以用 `--list-id`。

任务导出文件默认被 git 忽略，因为里面可能包含项目 id、用户 id、任务 id 等隐私信息。

Telegram bot 也会复用同一套 Teambition client 做空闲任务提示。比如 Codex 当前有空闲额度、某个看板还有待办时，bot 可以把待办摘要发出来，你就能基于真实 backlog 继续远程派活，而不是临时想一个模糊任务。

## Windows 开机启动

可以把 `start_remote_codex_bot.bat` 放进当前用户的启动目录，或者在启动目录里创建指向它的快捷方式：

```text
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
```

这个 bat 会设置 Python UTF-8 相关环境变量，并写入带时间戳的日志文件。

## 安全注意事项

- 不要提交 `.env`。
- 不要提交 `logs/`。
- 任何进入过旧日志、shell 历史、截图或 git 历史的 token 都建议旋转。
- 使用 `TELEGRAM_ALLOWED_CHAT_IDS` 限制谁可以远程触发本机 Codex。
- 这个 bot 会以较高本地权限启动 Codex。只应在可信机器上运行，并只开放给可信 Telegram 账号。

## License

本项目使用 [MIT License](LICENSE) 开源。
