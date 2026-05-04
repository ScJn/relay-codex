#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Telegram worker that runs Codex tasks on this Windows machine."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

try:
    from teambition_project_tasks import (
        DEFAULT_PROJECT_ID,
        TeambitionClient,
        annotate_tasks,
        build_config,
        filter_tasks,
        get_board_list_maps,
        resolve_board_list_ids,
    )
except ModuleNotFoundError:
    from scripts.teambition_project_tasks import (
        DEFAULT_PROJECT_ID,
        TeambitionClient,
        annotate_tasks,
        build_config,
        filter_tasks,
        get_board_list_maps,
        resolve_board_list_ids,
    )


PROJECTS = {
    "/f2": Path(r"C:\Users\Lenovo\PycharmProjects\F\f2"),
    "/f3": Path(r"C:\Users\Lenovo\PycharmProjects\F\f3"),
    "/f4": Path(r"C:\Users\Lenovo\CLionProjects\f4"),
    "/f5": Path(r"C:\Users\Lenovo\PycharmProjects\F\f5"),
}

RISKY_KEYWORDS = (
    "删除",
    "删掉",
    "Remove-Item",
    "rm -rf",
    "git push",
    "reset --hard",
    "安装",
    "pip install",
    "npm install",
    "danger-full-access",
    "dangerously-bypass-approvals-and-sandbox",
)

MAX_TELEGRAM_TEXT = 3900
CODEX_PERMISSION_MODE = "dangerously-bypass-approvals-and-sandbox"
STATE_FILE = Path(__file__).resolve().parents[1] / ".remote_codex_bot_state.json"
LOCK_FILE = Path(__file__).resolve().parents[1] / ".remote_codex_bot.lock"
COMMAND_PLACEHOLDER_INTERVAL_SECONDS = 8.0
AUTO_STATUS_INTERVAL_SECONDS = 30 * 60
IDLE_PRIMARY_REMAINING_THRESHOLD_PERCENT = 50.0
DEFAULT_MAX_PARALLEL_TASKS = 3
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"


def resolve_codex_command() -> str:
    candidates = ("codex.cmd", "codex.exe", "codex")
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise FileNotFoundError("Unable to find codex.cmd, codex.exe, or codex on PATH")


def reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class Task:
    task_id: str
    chat_id: int
    project_key: str
    project_dir: Path
    prompt: str
    session_id: str | None = None
    goal_objective: str | None = None


@dataclass
class RunningTask:
    task: Task
    process: subprocess.Popen[str] | None
    started_at: float
    cancelled: bool = False
    guidance: list[str] | None = None


@dataclass
class TaskProgress:
    sent_agent_texts: set[str]
    command_started_count: int = 0
    command_completed_count: int = 0
    last_placeholder_sent_at: float = 0.0
    last_agent_text: str = ""
    sent_any_agent_text: bool = False


class TelegramCodexBot:
    def __init__(
        self,
        token: str,
        allowed_chat_ids: set[int],
        poll_timeout: int = 30,
        max_parallel_tasks: int = DEFAULT_MAX_PARALLEL_TASKS,
    ) -> None:
        self.token = token
        self.allowed_chat_ids = allowed_chat_ids
        self.poll_timeout = poll_timeout
        self.max_parallel_tasks = max_parallel_tasks
        self.api_base = f"https://api.telegram.org/bot{token}"
        self.offset = 0
        self.pending: dict[str, Task] = {}
        self.active_projects: dict[int, str] = {}
        self.current_tasks: dict[int, str] = {}
        self.new_task_requested: set[int] = set()
        self.sessions: dict[str, str] = {}
        self.running_tasks: dict[str, RunningTask] = {}
        self.lock = threading.Lock()
        self._stop_event = threading.Event()
        self._load_state()

    def run(self) -> None:
        self._log("Telegram Codex bot started. Press Ctrl+C to stop.")
        self._start_auto_status_thread()
        while True:
            try:
                for update in self._get_updates():
                    self._handle_update(update)
            except KeyboardInterrupt:
                self._log("Stopping.")
                self._stop_event.set()
                self._cancel_running(notify=True)
                return
            except Exception as exc:  # Keep the long-running worker alive.
                self._log(f"Polling error: {exc!r}")
                time.sleep(5)

    def _start_auto_status_thread(self) -> None:
        thread = threading.Thread(target=self._auto_status_loop, daemon=True)
        thread.start()

    def _auto_status_loop(self) -> None:
        while not self._stop_event.wait(AUTO_STATUS_INTERVAL_SECONDS):
            try:
                self._send_auto_status()
            except Exception as exc:
                self._log(f"Auto status error: {exc!r}")

    def _send_auto_status(self) -> None:
        with self.lock:
            is_idle = not self.running_tasks
        snapshot = self._load_latest_codex_rate_limits()
        remaining_percent = self._get_primary_remaining_percent(snapshot)

        for chat_id in sorted(self.allowed_chat_ids):
            self._send_message(chat_id, self._build_status_text(chat_id, snapshot=snapshot))

        if not is_idle or remaining_percent is None or remaining_percent <= IDLE_PRIMARY_REMAINING_THRESHOLD_PERCENT:
            return

        idle_message = self._build_idle_work_request(remaining_percent)
        for chat_id in sorted(self.allowed_chat_ids):
            self._send_message(chat_id, idle_message)

    def _get_updates(self) -> list[dict[str, Any]]:
        payload = {
            "offset": self.offset,
            "timeout": self.poll_timeout,
            "allowed_updates": json.dumps(["message"]),
        }
        response = requests.get(f"{self.api_base}/getUpdates", params=payload, timeout=self.poll_timeout + 10)
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(body)
        updates = body.get("result", [])
        if updates:
            self.offset = max(update["update_id"] for update in updates) + 1
        return updates

    def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = (message.get("text") or "").strip()

        if not isinstance(chat_id, int) or not text:
            return

        if chat_id not in self.allowed_chat_ids:
            self._log(f"Rejected message from unauthorized chat_id={chat_id}: {text[:80]}")
            return

        if text == "/help" or text.startswith("/start"):
            self._send_help(chat_id)
        elif text == "/projects":
            self._send_projects(chat_id)
        elif text == "/status":
            self._send_status(chat_id)
        elif text == "/goal" or text.startswith("/goal "):
            self._goal_command(chat_id, text)
        elif text.startswith("/cancel"):
            self._cancel_command(chat_id, text)
        elif text in {"New Chat", "/new_chat"}:
            self._new_chat(chat_id)
        elif text.startswith("/guide"):
            self._guide_running_task(chat_id, text)
        elif text.startswith("/approve"):
            self._approve(chat_id, text)
        elif text.startswith("/reject"):
            self._reject(chat_id, text)
        else:
            self._maybe_start_task(chat_id, text)

    def _goal_command(self, chat_id: int, text: str) -> None:
        _, _, goal = text.partition(" ")
        goal = goal.strip()
        if not goal:
            project_key = self.active_projects.get(chat_id)
            project_hint = f"当前默认工程：{project_key}" if project_key else "当前还没有选择默认工程"
            self._send_message(
                chat_id,
                "用法：/goal <目标内容>\n"
                f"{project_hint}\n"
                "示例：/goal 修复当前 Bot 的状态输出，并跑配置检查",
            )
            return
        self._maybe_start_task(chat_id, goal, goal_objective=goal)

    def _guide_running_task(self, chat_id: int, text: str) -> None:
        parts = text.split(maxsplit=2)
        if len(parts) != 3:
            self._send_message(chat_id, "用法：/guide <任务id> <补充内容>")
            return
        task_id = parts[1].strip()
        guidance = parts[2].strip()
        if not guidance:
            self._send_message(chat_id, "补充内容不能为空。")
            return
        with self.lock:
            running = self.running_tasks.get(task_id)
            if running is None or running.task.chat_id != chat_id:
                guidance_count = None
            else:
                guidance_count = self._enqueue_guidance_locked(task_id, guidance)
        if guidance_count is None:
            self._send_message(chat_id, f"没有找到运行中的任务：{task_id}")
            return
        self._send_message(
            chat_id,
            f"已加入任务 {task_id} 的引导队列。\n"
            f"排队引导数：{guidance_count}\n"
            "当前 Codex turn 结束后会自动继续处理。",
        )

    def _maybe_start_task(self, chat_id: int, text: str, goal_objective: str | None = None) -> None:
        parsed = self._parse_project_command(text)
        if parsed is None:
            project_key = self.active_projects.get(chat_id)
            if project_key is None:
                self._send_message(chat_id, "请先选择工程：/f2、/f3、/f4 或 /f5；也可以直接发送 /f5 任务内容。")
                return
            prompt = text
        else:
            project_key, prompt = parsed
            self.active_projects[chat_id] = project_key
            self._save_state()

        if not prompt:
            if chat_id in self.new_task_requested:
                self._send_message(chat_id, f"已选择工程：{project_key} -> {PROJECTS[project_key]}\n现在发送任务内容，会创建新的 Codex thread。")
            else:
                session_id = self._get_session_id(chat_id, project_key)
                mode = f"会继续最近会话：{session_id}" if session_id else "当前没有最近会话，下一条任务会创建新会话"
                self._send_message(chat_id, f"已选择工程：{project_key} -> {PROJECTS[project_key]}\n{mode}。")
            return

        if self._route_as_guidance(chat_id, prompt):
            return

        task = Task(
            task_id=uuid.uuid4().hex[:8],
            chat_id=chat_id,
            project_key=project_key,
            project_dir=PROJECTS[project_key],
            prompt=prompt,
            session_id=self._get_session_id_for_new_task(chat_id, project_key),
            goal_objective=goal_objective,
        )

        if not task.project_dir.exists():
            self._send_message(chat_id, f"项目目录不存在：{task.project_dir}")
            return

        if self._is_risky(prompt):
            self.pending[task.task_id] = task
            self.new_task_requested.discard(chat_id)
            self._send_message(
                chat_id,
                "检测到高危操作，任务已暂停。\n"
                f"任务ID：{task.task_id}\n"
                f"项目：{task.project_key} -> {task.project_dir}\n"
                f"任务：{task.prompt}\n\n"
                f"确认执行请回复：/approve {task.task_id}\n"
                f"放弃执行请回复：/reject {task.task_id}",
            )
            return

        self.new_task_requested.discard(chat_id)
        self._start_task(task)

    def _route_as_guidance(self, chat_id: int, text: str) -> bool:
        with self.lock:
            current_task_id = self.current_tasks.get(chat_id)
            running = self.running_tasks.get(current_task_id or "") if current_task_id else None
            if running is None or running.task.chat_id != chat_id:
                chat_tasks = [item for item in self.running_tasks.values() if item.task.chat_id == chat_id]
                if len(chat_tasks) == 1:
                    running = chat_tasks[0]
                    self.current_tasks[chat_id] = running.task.task_id
                elif len(chat_tasks) > 1:
                    task_ids = ", ".join(sorted(item.task.task_id for item in chat_tasks))
                    running = None
                else:
                    task_ids = ""

            if running is not None:
                guidance_count = self._enqueue_guidance_locked(running.task.task_id, text)
                task_id = running.task.task_id
            else:
                guidance_count = None
                task_id = ""

        if guidance_count is not None:
            self._send_message(
                chat_id,
                f"已加入任务 {task_id} 的引导队列。\n"
                f"排队引导数：{guidance_count}\n"
                "普通消息会优先补充到运行中的任务；任务结束后会按最近会话继续。",
            )
            return True
        if task_ids:
            self._send_message(chat_id, f"当前有多个运行中任务，请指定：/guide <任务id> <内容>\n任务：{task_ids}")
            return True
        return False

    def _get_session_id_for_new_task(self, chat_id: int, project_key: str) -> str | None:
        if chat_id in self.new_task_requested:
            return None
        session_id = self._get_session_id(chat_id, project_key)
        if not session_id:
            return None
        with self.lock:
            session_is_busy = any(
                running.task.chat_id == chat_id
                and running.task.project_key == project_key
                and running.task.session_id == session_id
                for running in self.running_tasks.values()
            )
        if session_is_busy:
            return None
        return session_id

    def _enqueue_guidance_locked(self, task_id: str, text: str) -> int | None:
        running = self.running_tasks.get(task_id)
        if running is None:
            return None
        if running.guidance is None:
            running.guidance = []
        running.guidance.append(text.strip())
        return len(running.guidance)

    def _parse_project_command(self, text: str) -> tuple[str, str] | None:
        command, _, rest = text.partition(" ")
        command = command.strip()
        if command not in PROJECTS:
            return None
        return command, rest.strip()

    def _session_key(self, chat_id: int, project_key: str) -> str:
        return f"{chat_id}:{project_key}"

    def _get_session_id(self, chat_id: int, project_key: str) -> str | None:
        return self.sessions.get(self._session_key(chat_id, project_key))

    def _set_session_id(self, chat_id: int, project_key: str, session_id: str) -> None:
        self.sessions[self._session_key(chat_id, project_key)] = session_id
        self._save_state()

    def _new_chat(self, chat_id: int) -> None:
        self.new_task_requested.add(chat_id)
        project_key = self.active_projects.get(chat_id)
        if project_key is None:
            self._send_message(chat_id, "已准备开启新任务。请先选择工程：/f2、/f3、/f4 或 /f5；也可以直接发送 /f3 任务内容。")
            return
        self.sessions.pop(self._session_key(chat_id, project_key), None)
        self._save_state()
        self._send_message(chat_id, f"已为 {project_key} 开启新会话。下一条消息会启动一个新任务并创建新的 Codex thread。")

    def _is_risky(self, prompt: str) -> bool:
        lowered = prompt.lower()
        return any(keyword.lower() in lowered for keyword in RISKY_KEYWORDS)

    def _approve(self, chat_id: int, text: str) -> None:
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            self._send_message(chat_id, "用法：/approve <任务id>")
            return
        task_id = parts[1].strip()
        task = self.pending.get(task_id)
        if task is None or task.chat_id != chat_id:
            self._send_message(chat_id, f"没有找到待审批任务：{task_id}")
            return
        self.pending.pop(task_id, None)
        self._start_task(task)

    def _reject(self, chat_id: int, text: str) -> None:
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            self._send_message(chat_id, "用法：/reject <任务id>")
            return
        task_id = parts[1].strip()
        task = self.pending.get(task_id)
        if task is None or task.chat_id != chat_id:
            self._send_message(chat_id, f"没有找到待审批任务：{task_id}")
            return
        self.pending.pop(task_id, None)
        self._send_message(chat_id, f"已放弃任务：{task_id}")

    def _start_task(self, task: Task) -> None:
        with self.lock:
            running_count = len(self.running_tasks)
        if self.max_parallel_tasks > 0 and running_count >= self.max_parallel_tasks:
            self._send_message(
                task.chat_id,
                f"当前运行中任务已达到上限：{running_count}/{self.max_parallel_tasks}。\n"
                "稍后再发，或先用 /cancel <任务id> 取消一个任务。",
            )
            return

        with self.lock:
            self.running_tasks[task.task_id] = RunningTask(task=task, process=None, started_at=time.time())
            self.current_tasks[task.chat_id] = task.task_id

        self._send_message(task.chat_id, self._build_start_message(task))
        thread = threading.Thread(target=self._run_app_server_task, args=(task,), daemon=True)
        thread.start()

    def _build_start_message(self, task: Task) -> str:
        mode = f"继续交互线程：{task.session_id}" if task.session_id else "新交互线程"
        goal = f"\nGoal：{task.goal_objective}" if task.goal_objective else ""
        return (
            f"已启动任务 {task.task_id}\n"
            f"项目：{task.project_key} -> {task.project_dir}\n"
            f"模式：{mode}{goal}\n"
            f"普通消息会继续这个任务；发送 /status 查看状态，/cancel {task.task_id} 取消。\n"
            "只有发送 /new_chat 才会重置下一次任务的上下文。"
        )

    def _run_app_server_task(self, task: Task) -> None:
        progress = TaskProgress(sent_agent_texts=set())
        thread_id: str | None = task.session_id
        error_message = ""
        process: subprocess.Popen[str] | None = None
        try:
            thread_id, process = asyncio.run(self._run_app_server_turn(task, progress))
        except Exception as exc:
            error_message = self._compact_error_message(str(exc))
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()

        with self.lock:
            running = self.running_tasks.get(task.task_id)
            cancelled = bool(running and running.cancelled)
            queued_guidance = list(running.guidance or []) if running else []
            if running:
                self.running_tasks.pop(task.task_id, None)
            if self.current_tasks.get(task.chat_id) == task.task_id:
                self.current_tasks.pop(task.chat_id, None)

        if cancelled:
            self._send_message(task.chat_id, f"任务已取消：{task.task_id}")
            return

        if thread_id:
            self._set_session_id(task.chat_id, task.project_key, thread_id)

        if error_message:
            detail = f"\n\n最后回复：\n{progress.last_agent_text}" if progress.last_agent_text else ""
            self._send_message(task.chat_id, f"任务失败：{task.task_id}\n错误：{error_message}{detail}")
        else:
            self._send_message(task.chat_id, self._build_completion_summary(task, progress))

        if queued_guidance:
            self._start_queued_guidance(task, thread_id, queued_guidance)

    async def _run_app_server_turn(self, task: Task, progress: TaskProgress) -> tuple[str | None, subprocess.Popen[str]]:
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("缺少 websockets 依赖，无法使用 Codex app-server 交互模式。") from exc

        port = reserve_loopback_port()
        url = f"ws://127.0.0.1:{port}"
        process = subprocess.Popen(
            [resolve_codex_command(), "app-server", "--enable", "goals", "--listen", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        with self.lock:
            running = self.running_tasks.get(task.task_id)
            if running:
                running.process = process

        try:
            ws = await self._connect_app_server(url)
            try:
                await self._rpc(ws, "initialize", {
                    "clientInfo": {"name": "f3-telegram-bot", "title": "F3 Telegram Bot", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                })
                thread_id = await self._open_codex_thread(ws, task)
                if task.goal_objective:
                    await self._rpc(ws, "thread/goal/set", {
                        "threadId": thread_id,
                        "objective": task.goal_objective,
                        "status": "active",
                    })
                    self._send_message(task.chat_id, f"已设置 Codex goal：{task.goal_objective}")

                turn = await self._rpc(ws, "turn/start", {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": task.prompt, "text_elements": []}],
                    "cwd": str(task.project_dir),
                    "approvalPolicy": "never",
                    "sandboxPolicy": {"type": "dangerFullAccess"},
                })
                turn_id = ((turn.get("turn") or {}).get("id") if isinstance(turn, dict) else None)
                await self._watch_app_server_notifications(ws, task, progress, thread_id, turn_id)
                return thread_id, process
            finally:
                await self._close_app_server_connection(ws)
        except Exception:
            if process.poll() is None:
                process.terminate()
            raise

    async def _connect_app_server(self, url: str) -> Any:
        import websockets

        last_error: Exception | None = None
        for _ in range(30):
            try:
                return await websockets.connect(url, open_timeout=1)
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(0.2)
        raise RuntimeError(f"无法连接 Codex app-server：{last_error!r}")

    async def _close_app_server_connection(self, ws: Any) -> None:
        close = getattr(ws, "close", None)
        if close is None:
            return

        result = close()
        if inspect.isawaitable(result):
            await result

        wait_closed = getattr(ws, "wait_closed", None)
        if wait_closed is not None:
            result = wait_closed()
            if inspect.isawaitable(result):
                await result

    async def _rpc(self, ws: Any, method: str, params: Any) -> Any:
        request_id = uuid.uuid4().hex
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        await ws.send(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        while True:
            message = json.loads(await ws.recv())
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message.get("error") or {}
                raise RuntimeError(error.get("message") if isinstance(error, dict) else str(error))
            return message.get("result")

    async def _open_codex_thread(self, ws: Any, task: Task) -> str:
        params: dict[str, Any] = {
            "cwd": str(task.project_dir),
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
            "config": {"features": {"goals": True}},
            "persistExtendedHistory": True,
        }
        if task.session_id:
            params["threadId"] = task.session_id
            params["excludeTurns"] = True
            result = await self._rpc(ws, "thread/resume", params)
        else:
            params["experimentalRawEvents"] = False
            result = await self._rpc(ws, "thread/start", params)
        thread = result.get("thread") if isinstance(result, dict) else None
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str) or not thread_id:
            raise RuntimeError("Codex app-server 没有返回 thread id。")
        return thread_id

    async def _watch_app_server_notifications(
        self,
        ws: Any,
        task: Task,
        progress: TaskProgress,
        thread_id: str,
        turn_id: str | None,
    ) -> None:
        while True:
            with self.lock:
                running = self.running_tasks.get(task.task_id)
                if running and running.cancelled:
                    await self._rpc(ws, "turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
                    return

            message = json.loads(await ws.recv())
            method = message.get("method")
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            if params.get("threadId") != thread_id:
                continue

            if method == "item/completed":
                item = params.get("item") if isinstance(params, dict) else None
                if isinstance(item, dict) and item.get("type") == "agentMessage":
                    self._handle_agent_text(task, progress, str(item.get("text") or ""))
                elif isinstance(item, dict) and item.get("type") == "commandExecution":
                    self._handle_command_completed(task, progress)
            elif method == "item/started":
                item = params.get("item") if isinstance(params, dict) else None
                if isinstance(item, dict) and item.get("type") == "commandExecution":
                    self._handle_command_started(task, progress)
            elif method == "turn/completed":
                turn = params.get("turn") if isinstance(params, dict) else {}
                status = turn.get("status") if isinstance(turn, dict) else None
                if isinstance(status, dict) and status.get("type") == "failed":
                    error = turn.get("error") if isinstance(turn, dict) else None
                    raise RuntimeError(error.get("message") if isinstance(error, dict) else "Codex turn 执行失败。")
                return
            elif method == "error":
                message_text = params.get("message") if isinstance(params, dict) else None
                if isinstance(message_text, str) and message_text:
                    raise RuntimeError(message_text)

    def _start_queued_guidance(self, previous_task: Task, thread_id: str | None, queued_guidance: list[str]) -> None:
        session_id = thread_id or self._get_session_id(previous_task.chat_id, previous_task.project_key)
        if not session_id:
            self._send_message(previous_task.chat_id, "已收到后续引导，但当前任务没有可继续的 Codex 会话。请重新发送任务。")
            return

        prompt = self._format_guidance_prompt(queued_guidance)
        task = Task(
            task_id=uuid.uuid4().hex[:8],
            chat_id=previous_task.chat_id,
            project_key=previous_task.project_key,
            project_dir=previous_task.project_dir,
            prompt=prompt,
            session_id=session_id,
        )
        self._send_message(previous_task.chat_id, f"开始处理排队引导：{len(queued_guidance)} 条")
        self._start_task(task)

    def _format_guidance_prompt(self, queued_guidance: list[str]) -> str:
        if len(queued_guidance) == 1:
            return queued_guidance[0]
        lines = ["用户在你执行上一轮任务期间补充了以下引导，请结合已有上下文继续："]
        for index, guidance in enumerate(queued_guidance, start=1):
            lines.append(f"{index}. {guidance}")
        return "\n".join(lines)

    def _handle_agent_text(self, task: Task, progress: TaskProgress, text: str) -> None:
        text = text.strip()
        if not text or text in progress.sent_agent_texts:
            return
        progress.sent_agent_texts.add(text)
        progress.last_agent_text = text
        progress.sent_any_agent_text = True
        self._send_message(task.chat_id, text)

    def _handle_command_started(self, task: Task, progress: TaskProgress) -> None:
        progress.command_started_count += 1
        if progress.command_started_count == 1:
            self._send_command_placeholder(task, progress, "正在执行命令...")
            return
        self._send_command_placeholder_throttled(task, progress, "正在处理代码/文件操作...")

    def _handle_command_completed(self, task: Task, progress: TaskProgress) -> None:
        progress.command_completed_count += 1
        self._send_command_placeholder_throttled(
            task,
            progress,
            f"命令执行完成 {progress.command_completed_count} 个",
        )

    def _send_command_placeholder_throttled(self, task: Task, progress: TaskProgress, message: str) -> None:
        now = time.time()
        if now - progress.last_placeholder_sent_at < COMMAND_PLACEHOLDER_INTERVAL_SECONDS:
            return
        self._send_command_placeholder(task, progress, message)

    def _send_command_placeholder(self, task: Task, progress: TaskProgress, message: str) -> None:
        progress.last_placeholder_sent_at = time.time()
        self._send_message(task.chat_id, message)

    def _compact_error_message(self, message: str) -> str:
        try:
            parsed = json.loads(message)
        except json.JSONDecodeError:
            return message.strip()

        if isinstance(parsed, dict):
            error = parsed.get("error")
            if isinstance(error, dict):
                nested = error.get("message")
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
            nested = parsed.get("message")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
        return message.strip()

    def _build_completion_summary(self, task: Task, progress: TaskProgress) -> str:
        command_summary = ""
        if progress.command_started_count or progress.command_completed_count:
            command_summary = (
                f"\n命令执行：启动 {progress.command_started_count} 个，完成 {progress.command_completed_count} 个"
            )

        if progress.sent_any_agent_text:
            body = f"任务完成：{task.task_id}\n项目：{task.project_key}{command_summary}"
        else:
            body = f"任务完成：{task.task_id}\n项目：{task.project_key}{command_summary}\n没有捕获到 Codex 的文本回复。"
        return body

    def _send_help(self, chat_id: int) -> None:
        self._send_message(
            chat_id,
            "可用命令：\n"
            "/projects - 查看项目目录\n"
            "/status - 查看当前运行任务\n"
            "/goal <目标> - 用目标模式启动或继续 Codex 任务\n"
            "/new_chat - 只重置下一条任务的上下文，创建新的 Codex 会话\n"
            "/cancel <任务id> - 取消指定任务；只有一个任务时 /cancel 也可用\n"
            "/guide <任务id> <内容> - 给运行中的任务补充引导\n"
            "/f2 /f3 /f4 /f5 - 选择默认工程\n"
            "/approve <任务id> - 批准高危任务\n"
            "/reject <任务id> - 放弃高危任务\n\n"
            "当前 Codex 执行模式：全权限、无沙盒。\n\n"
            "发任务示例：\n"
            "/f2 看一下这个项目有哪些工具文档\n"
            "/f3 检查 Telegram bot 脚本结构\n"
            "/f4 说明项目结构\n"
            "/f5 修复策略回放页面的错误，并跑测试",
        )

    def _send_projects(self, chat_id: int) -> None:
        lines = ["项目目录："]
        for key, path in PROJECTS.items():
            exists = "OK" if path.exists() else "MISSING"
            lines.append(f"{key} -> {path} [{exists}]")
        self._send_message(chat_id, "\n".join(lines))

    def _send_status(self, chat_id: int) -> None:
        self._send_message(chat_id, self._build_status_text(chat_id))

    def _build_status_text(self, chat_id: int, snapshot: dict[str, Any] | None = None) -> str:
        with self.lock:
            running_tasks = [
                running
                for running in self.running_tasks.values()
                if running.task.chat_id == chat_id
            ]
            pending = ", ".join(sorted(task_id for task_id, task in self.pending.items() if task.chat_id == chat_id)) or "无"
        active_project = self.active_projects.get(chat_id)
        session_id = self._get_session_id(chat_id, active_project) if active_project else None
        codex_balance = self._format_codex_balance(snapshot=snapshot)
        active_text = (
            f"当前默认工程：{active_project}\n当前会话：{session_id or '新会话'}\n"
            if active_project
            else "当前默认工程：未选择\n当前会话：无\n"
        )
        if not running_tasks:
            return f"{active_text}{codex_balance}\n当前没有运行中的任务。\n待审批任务：{pending}"
        lines = [
            active_text + codex_balance,
            f"运行中任务：{len(running_tasks)} 条",
        ]
        now = time.time()
        for running in sorted(running_tasks, key=lambda item: item.started_at):
            elapsed = int(now - running.started_at)
            task = running.task
            guidance_count = len(running.guidance or [])
            prompt = task.prompt if len(task.prompt) <= 120 else task.prompt[:117] + "..."
            lines.append(
                f"- {task.task_id} {task.project_key} 耗时 {elapsed} 秒，引导 {guidance_count} 条：{prompt}"
            )
        lines.append(f"待审批任务：{pending}")
        return "\n".join(lines)

    def _format_codex_balance(self, snapshot: dict[str, Any] | None = None) -> str:
        if snapshot is None:
            snapshot = self._load_latest_codex_rate_limits()
        if snapshot is None:
            return "Codex 额度：暂未读取到本地用量记录"

        rate_limits = snapshot.get("rate_limits") or {}
        primary = rate_limits.get("primary") if isinstance(rate_limits, dict) else None
        secondary = rate_limits.get("secondary") if isinstance(rate_limits, dict) else None
        credits = rate_limits.get("credits") if isinstance(rate_limits, dict) else None
        plan_type = rate_limits.get("plan_type") if isinstance(rate_limits, dict) else None

        lines = ["Codex 额度："]
        if plan_type:
            lines.append(f"套餐：{plan_type}")
        lines.append(f"5小时额度：{self._format_rate_limit_window(primary)}")
        lines.append(f"1周余额：{self._format_rate_limit_window(secondary)}")
        lines.append(f"余额：{self._format_codex_credits(credits)}")
        recorded_at = snapshot.get("timestamp")
        if isinstance(recorded_at, str) and recorded_at:
            lines.append(f"更新时间：{recorded_at}")
        return "\n".join(lines)

    def _get_primary_remaining_percent(self, snapshot: dict[str, Any] | None = None) -> float | None:
        if snapshot is None:
            snapshot = self._load_latest_codex_rate_limits()
        if snapshot is None:
            return None
        rate_limits = snapshot.get("rate_limits")
        if not isinstance(rate_limits, dict):
            return None
        primary = rate_limits.get("primary")
        if not isinstance(primary, dict):
            return None
        used_percent = primary.get("used_percent")
        if not isinstance(used_percent, (int, float)):
            return None
        return max(0.0, 100.0 - float(used_percent))

    def _build_idle_work_request(self, remaining_percent: float) -> str:
        board_list_name = os.getenv("TEAMBITION_IDLE_BOARD_LIST", "doing").strip() or "doing"
        try:
            tasks = self._fetch_teambition_board_tasks(board_list_name)
        except Exception as exc:
            return (
                "我现在很闲，老板给我派活吧\n\n"
                f"5小时额度剩余 {remaining_percent:.1f}%。\n"
                f"读取 {board_list_name} 任务失败：{exc!r}"
            )

        lines = [
            "我现在很闲，老板给我派活吧",
            "",
            f"5小时额度剩余 {remaining_percent:.1f}%。",
            f"{board_list_name} 未完成任务共 {len(tasks)} 条：",
        ]
        if not tasks:
            lines.append(f"暂无 {board_list_name} 任务。")
            return "\n".join(lines)

        for index, task in enumerate(tasks, start=1):
            title = task.get("content") or "(no title)"
            priority = task.get("priority", 0)
            lines.append(f"{index}. P{priority} {title}")
        return "\n".join(lines)

    def _fetch_teambition_board_tasks(self, board_list_name: str) -> list[dict[str, Any]]:
        client = TeambitionClient(build_config(DEFAULT_PROJECT_ID))
        name_to_board_list, id_to_board_list, _ = get_board_list_maps(client)
        required_board_list_ids = resolve_board_list_ids(name_to_board_list, [board_list_name], [])
        tasks = filter_tasks(
            client.query_project_tasks(),
            required_board_list_ids=required_board_list_ids,
            min_priority=None,
            include_archived=False,
        )
        return annotate_tasks(tasks, id_to_board_list)

    def _load_latest_codex_rate_limits(self) -> dict[str, Any] | None:
        if not CODEX_SESSIONS_DIR.exists():
            return None

        candidates: list[tuple[float, Path]] = []
        try:
            for path in CODEX_SESSIONS_DIR.rglob("*.jsonl"):
                try:
                    candidates.append((path.stat().st_mtime, path))
                except OSError:
                    continue
        except OSError:
            return None

        for _, path in sorted(candidates, reverse=True):
            latest: dict[str, Any] | None = None
            try:
                with path.open("r", encoding="utf-8", errors="replace") as file:
                    for line in file:
                        if '"rate_limits"' not in line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        payload = event.get("payload")
                        if not isinstance(payload, dict) or payload.get("type") != "token_count":
                            continue
                        rate_limits = payload.get("rate_limits")
                        if isinstance(rate_limits, dict):
                            latest = {
                                "timestamp": self._format_codex_timestamp(event.get("timestamp")),
                                "rate_limits": rate_limits,
                            }
            except OSError:
                continue
            if latest is not None:
                return latest
        return None

    def _format_rate_limit_window(self, window: Any) -> str:
        if not isinstance(window, dict):
            return "未知"
        used_percent = window.get("used_percent")
        reset_text = self._format_reset_at(window.get("resets_at"))
        if isinstance(used_percent, (int, float)):
            remaining_percent = max(0.0, 100.0 - float(used_percent))
            return f"剩余 {remaining_percent:.1f}%（已用 {float(used_percent):.1f}%，重置 {reset_text}）"
        return f"未知（重置 {reset_text}）"

    def _format_codex_credits(self, credits: Any) -> str:
        if not isinstance(credits, dict):
            return "未返回"
        if credits.get("unlimited"):
            return "无限"
        balance = credits.get("balance")
        if balance is None:
            return "未返回"
        return str(balance)

    def _format_reset_at(self, resets_at: Any) -> str:
        if not isinstance(resets_at, (int, float)):
            return "未知"
        try:
            return datetime.fromtimestamp(float(resets_at)).strftime("%m-%d %H:%M")
        except (OSError, OverflowError, ValueError):
            return "未知"

    def _format_codex_timestamp(self, timestamp: Any) -> str:
        if not isinstance(timestamp, str) or not timestamp:
            return ""
        try:
            normalized = timestamp.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized).astimezone().strftime("%m-%d %H:%M:%S")
        except ValueError:
            return timestamp

    def _cancel_command(self, chat_id: int, text: str) -> None:
        parts = text.split(maxsplit=1)
        task_id = parts[1].strip() if len(parts) == 2 else None
        self._cancel_running(notify=True, chat_id=chat_id, task_id=task_id)

    def _cancel_running(self, notify: bool, chat_id: int | None = None, task_id: str | None = None) -> None:
        ambiguous_message: str | None = None
        with self.lock:
            if task_id:
                running = self.running_tasks.get(task_id)
                if running is None or (chat_id is not None and running.task.chat_id != chat_id):
                    targets: list[RunningTask] = []
                else:
                    targets = [running]
            elif chat_id is None:
                targets = list(self.running_tasks.values())
            else:
                chat_tasks = [running for running in self.running_tasks.values() if running.task.chat_id == chat_id]
                if len(chat_tasks) == 1:
                    targets = chat_tasks
                elif len(chat_tasks) == 0:
                    targets = []
                else:
                    ids = ", ".join(sorted(running.task.task_id for running in chat_tasks))
                    ambiguous_message = f"当前有多个运行中任务，请指定：/cancel <任务id>\n任务：{ids}"
                    targets = []

            for running in targets:
                running.cancelled = True

        if ambiguous_message:
            if notify and chat_id is not None:
                self._send_message(chat_id, ambiguous_message)
            return

        if not targets:
            if notify and chat_id is not None:
                if task_id:
                    self._send_message(chat_id, f"没有找到运行中的任务：{task_id}")
                else:
                    self._send_message(chat_id, "当前没有运行中的任务。")
            return

        for running in targets:
            try:
                if running.process is None:
                    pass
                elif os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(running.process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                elif running.process.poll() is None:
                    running.process.terminate()
                    time.sleep(3)
                    if running.process.poll() is None:
                        running.process.kill()
            except Exception as exc:
                self._log(f"Cancel failed for {running.task.task_id}: {exc!r}")

            target_chat_id = chat_id or running.task.chat_id
            if notify and target_chat_id is not None:
                self._send_message(target_chat_id, f"已请求取消任务：{running.task.task_id}")

    def _send_message(self, chat_id: int, text: str) -> None:
        for chunk in self._split_text(text):
            payload = {"chat_id": chat_id, "text": chunk, "reply_markup": json.dumps(self._reply_keyboard())}
            try:
                response = requests.post(f"{self.api_base}/sendMessage", data=payload, timeout=20)
                response.raise_for_status()
            except Exception as exc:
                self._log(f"Failed to send Telegram message to {chat_id}: {exc!r}")

    def _reply_keyboard(self) -> dict[str, Any]:
        return {
            "keyboard": [
                [{"text": "/f2"}, {"text": "/f3"}],
                [{"text": "/f4"}, {"text": "/f5"}],
                [{"text": "/goal"}, {"text": "/new_chat"}, {"text": "/status"}],
                [{"text": "/cancel"}, {"text": "/projects"}],
            ],
            "resize_keyboard": True,
            "one_time_keyboard": False,
            "is_persistent": True,
        }

    def _split_text(self, text: str) -> list[str]:
        if len(text) <= MAX_TELEGRAM_TEXT:
            return [text]
        chunks: list[str] = []
        current = ""
        for line in text.splitlines(keepends=True):
            if len(current) + len(line) > MAX_TELEGRAM_TEXT:
                if current:
                    chunks.append(current)
                    current = ""
                while len(line) > MAX_TELEGRAM_TEXT:
                    chunks.append(line[:MAX_TELEGRAM_TEXT])
                    line = line[MAX_TELEGRAM_TEXT:]
            current += line
        if current:
            chunks.append(current)
        return chunks

    def _log(self, message: str) -> None:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {self._redact_secrets(message)}", flush=True)

    def _redact_secrets(self, text: str) -> str:
        text = re.sub(r"/bot\d+:[^/\s)'\"]+", "/bot<redacted>", text)
        if self.token:
            text = text.replace(self.token, "<telegram-bot-token>")
        return text

    def _load_state(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            self._log(f"Failed to load state file {STATE_FILE}: {exc!r}")
            return

        active_projects = data.get("active_projects")
        if isinstance(active_projects, dict):
            for chat_id, project_key in active_projects.items():
                if isinstance(project_key, str) and project_key in PROJECTS:
                    try:
                        self.active_projects[int(chat_id)] = project_key
                    except ValueError:
                        continue

        sessions = data.get("sessions")
        if isinstance(sessions, dict):
            self.sessions = {
                str(key): str(value)
                for key, value in sessions.items()
                if isinstance(key, str) and isinstance(value, str) and value.strip()
            }

    def _save_state(self) -> None:
        data = {
            "active_projects": {str(chat_id): project_key for chat_id, project_key in self.active_projects.items()},
            "sessions": self.sessions,
        }
        try:
            STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            self._log(f"Failed to save state file {STATE_FILE}: {exc!r}")


class SingleInstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def acquire(self) -> None:
        while True:
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode("ascii"))
                return
            except FileExistsError as exc:
                existing_pid = self._read_existing_pid()
                if existing_pid is None or not process_exists(existing_pid):
                    self._remove_stale_lock()
                    continue
                raise RuntimeError(
                    f"remote_codex_bot is already running with PID {existing_pid}. "
                    f"Stop that process first, or remove stale lock file: {self.path}"
                ) from exc

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            if self.path.exists():
                self.path.unlink()
        except OSError:
            pass

    def _read_existing_pid(self) -> int | None:
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
            return int(raw) if raw else None
        except (OSError, ValueError):
            return None

    def _remove_stale_lock(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    try:
        import ctypes
    except ImportError:
        return True

    process_query_limited_information = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def parse_allowed_chat_ids(raw: str) -> set[int]:
    chat_ids: set[int] = set()
    for item in raw.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            chat_ids.add(int(item))
        except ValueError as exc:
            raise ValueError(f"Invalid TELEGRAM_ALLOWED_CHAT_IDS value: {item!r}") from exc
    return chat_ids


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a Telegram bot that dispatches tasks to local Codex.")
    parser.add_argument("--env-file", default=".env", help="Path to the env file. Default: .env")
    parser.add_argument("--check-config", action="store_true", help="Validate configuration and exit.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    env_path = Path(args.env_file)
    if env_path.exists():
        load_dotenv(env_path)
    else:
        load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    allowed_chat_ids_raw = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    poll_timeout = int(os.getenv("TELEGRAM_POLL_TIMEOUT", "30"))
    max_parallel_tasks = int(os.getenv("TELEGRAM_MAX_PARALLEL_TASKS", str(DEFAULT_MAX_PARALLEL_TASKS)))

    errors: list[str] = []
    if not token:
        errors.append("Missing TELEGRAM_BOT_TOKEN")
    try:
        allowed_chat_ids = parse_allowed_chat_ids(allowed_chat_ids_raw)
    except ValueError as exc:
        errors.append(str(exc))
        allowed_chat_ids = set()
    if not allowed_chat_ids:
        errors.append("Missing TELEGRAM_ALLOWED_CHAT_IDS")

    for key, path in PROJECTS.items():
        if not path.exists():
            errors.append(f"Project path for {key} does not exist: {path}")

    try:
        codex_command = resolve_codex_command()
    except FileNotFoundError as exc:
        errors.append(str(exc))
        codex_command = ""

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2

    if args.check_config:
        print("Config OK")
        print("Codex command:", codex_command)
        print("Codex permission mode:", CODEX_PERMISSION_MODE)
        print("Max parallel tasks:", max_parallel_tasks)
        print("Allowed chat ids:", ", ".join(str(chat_id) for chat_id in sorted(allowed_chat_ids)))
        for key, path in PROJECTS.items():
            print(f"{key}: {path}")
        return 0

    lock = SingleInstanceLock(LOCK_FILE)
    try:
        lock.acquire()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3

    try:
        TelegramCodexBot(
            token=token,
            allowed_chat_ids=allowed_chat_ids,
            poll_timeout=poll_timeout,
            max_parallel_tasks=max_parallel_tasks,
        ).run()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
