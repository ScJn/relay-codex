#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Fetch unfinished tasks from one Teambition project."""

from __future__ import annotations

import argparse
import base64
import csv
import hmac
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


DEFAULT_PROJECT_ID = ""
BASE_URL = "https://open.teambition.com"


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def load_dotenv_if_present() -> None:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def generate_app_access_token(app_id: str, app_secret: str, expires_in_seconds: int = 3600) -> str:
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "_appId": app_id,
        "iat": now,
        "exp": now + expires_in_seconds,
    }
    signing_input = ".".join(
        [
            base64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            base64url(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
        ]
    )
    signature = hmac.new(app_secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{base64url(signature)}"


@dataclass(frozen=True)
class Config:
    token: str
    tenant_id: str
    project_id: str
    tenant_type: str = "organization"


class TeambitionClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {config.token}",
                "X-Tenant-Id": config.tenant_id,
                "X-Tenant-Type": config.tenant_type,
                "Content-Type": "application/json",
            }
        )

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_kwargs: dict[str, Any] = {"timeout": 30}
        if method.upper() == "GET":
            request_kwargs["params"] = payload or {}
        else:
            request_kwargs["json"] = payload or {}
        response = self.session.request(
            method,
            f"{BASE_URL}{path}",
            **request_kwargs,
        )
        response.raise_for_status()
        data = response.json()
        code = data.get("code")
        if code not in (None, 0, 200):
            raise RuntimeError(f"Teambition API error code={code}: {data.get('errorMessage')}")
        return data

    def query_project_tasks(self, page_size: int = 100, max_pages: int = 50) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        page_token = ""
        seen_page_tokens: set[str] = set()
        for _ in range(max_pages):
            payload: dict[str, Any] = {"pageSize": str(page_size)}
            if page_token:
                payload["pageToken"] = page_token
            data = self._request(
                "GET",
                f"/api/v3/project/{self.config.project_id}/task/query",
                payload,
            )
            tasks.extend(data.get("result") or [])
            page_token = data.get("nextPageToken") or ""
            if not page_token:
                return tasks
            if page_token in seen_page_tokens:
                raise RuntimeError(f"Repeated nextPageToken returned by Teambition: {page_token}")
            seen_page_tokens.add(page_token)
        raise RuntimeError(f"Stopped after {max_pages} pages; use a smaller filter or increase max_pages.")

    def query_board_lists(self) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            "/api/tasklist/query",
            {"projectId": self.config.project_id},
        )
        return data.get("result") or []

    def move_task(self, task: dict[str, Any], board_list_id: str) -> dict[str, Any]:
        task_id = str(task.get("id") or "")
        if not task_id:
            raise RuntimeError(f"Selected task has no id: {task!r}")
        data = self._request(
            "POST",
            "/api/task/update",
            {
                "operatorId": task.get("creatorId") or task.get("executorId"),
                "taskId": task_id,
                "projectId": task.get("projectId"),
                "tasklistId": board_list_id,
                "taskgroupId": task.get("tasklistId"),
                "content": task.get("content"),
                "executorId": task.get("executorId"),
                "statusId": task.get("tfsId") or task.get("statusId"),
                "startDate": task.get("startDate"),
                "dueDate": task.get("dueDate"),
                "note": task.get("note") or "",
                "priority": task.get("priority") or 0,
                "visible": task.get("visible") or "projectMembers",
                "participants": task.get("involveMembers") or task.get("participants") or [],
                "customfields": task.get("customfields") or [],
            },
        )
        return data.get("result") or {}


def build_config(project_id: str) -> Config:
    load_dotenv_if_present()
    project_id = project_id.strip() or os.getenv("TEAMBITION_PROJECT_ID", "").strip()
    token = os.getenv("TEAMBITION_APP_ACCESS_TOKEN")
    app_id = os.getenv("TEAMBITION_APP_ID") or os.getenv("TEAMBITION_APPLICATION_ID")
    app_secret = os.getenv("TEAMBITION_APP_SECRET") or os.getenv("TEAMBITION_SECRET")
    tenant_id = os.getenv("TEAMBITION_TENANT_ID")
    tenant_type = os.getenv("TEAMBITION_TENANT_TYPE", "organization")
    if not token and app_id and app_secret:
        token = generate_app_access_token(app_id, app_secret)
    missing = [
        name
        for name, value in (
            ("TEAMBITION_APP_ACCESS_TOKEN or TEAMBITION_APP_ID + TEAMBITION_APP_SECRET", token),
            ("TEAMBITION_TENANT_ID", tenant_id),
            ("TEAMBITION_PROJECT_ID", project_id),
        )
        if not value
    ]
    if missing:
        raise SystemExit("Missing required env vars: " + ", ".join(missing))
    return Config(token=token or "", tenant_id=tenant_id or "", tenant_type=tenant_type, project_id=project_id)


def get_board_list_maps(client: TeambitionClient) -> tuple[dict[str, str], dict[str, str], list[str]]:
    board_lists = client.query_board_lists()
    name_to_id: dict[str, str] = {}
    id_to_name: dict[str, str] = {}
    ordered_ids: list[str] = []
    for item in board_lists:
        name = item.get("name") or item.get("title")
        list_id = item.get("tasklistId") or item.get("id")
        if not name or not list_id:
            continue
        list_id = str(list_id)
        name_to_id[str(name)] = list_id
        id_to_name[list_id] = str(name)
        ordered_ids.append(list_id)
    return name_to_id, id_to_name, ordered_ids


def resolve_board_list_ids(
    name_to_id: dict[str, str],
    list_names: list[str],
    list_ids: list[str],
) -> set[str]:
    missing = [name for name in list_names if name not in name_to_id]
    if missing:
        available = ", ".join(sorted(name_to_id)) or "(no board lists returned)"
        raise SystemExit(f"Unknown board list name(s): {', '.join(missing)}. Available board lists: {available}")
    resolved = {name_to_id[name] for name in list_names}
    resolved.update(str(list_id) for list_id in list_ids)
    return resolved


def filter_tasks(
    tasks: list[dict[str, Any]],
    required_board_list_ids: set[str],
    min_priority: int | None,
    include_archived: bool,
) -> list[dict[str, Any]]:
    filtered = []
    for task in tasks:
        if task.get("isDone") is True:
            continue
        if not include_archived and task.get("isArchived") is True:
            continue
        if required_board_list_ids and str(task.get("stageId") or "") not in required_board_list_ids:
            continue
        if min_priority is not None and int(task.get("priority") or 0) < min_priority:
            continue
        filtered.append(task)
    return sorted(filtered, key=lambda item: (int(item.get("priority") or 0), item.get("updated") or ""), reverse=True)


def annotate_tasks(tasks: list[dict[str, Any]], id_to_board_list: dict[str, str]) -> list[dict[str, Any]]:
    annotated = []
    for task in tasks:
        item = dict(task)
        board_list_id = str(item.get("stageId") or "")
        item["boardListId"] = board_list_id
        item["boardListName"] = id_to_board_list.get(board_list_id, board_list_id)
        annotated.append(item)
    return annotated


def format_markdown(tasks: list[dict[str, Any]], ordered_board_list_ids: list[str]) -> str:
    lines = [f"# Teambition 未完成任务", "", f"共 {len(tasks)} 条", ""]
    current_list = None
    for task in tasks:
        board_list_name = task.get("boardListName") or task.get("boardListId") or ""
        if board_list_name != current_list:
            current_list = board_list_name
            lines.append(f"## {current_list or '未知列表'}")
            lines.append("")
        lines.append(f"- P{task.get('priority', 0)} {task.get('content') or '(no title)'}")
        lines.append(f"  - id: {task.get('id')}")
        lines.append(f"  - boardList: {board_list_name}")
        lines.append(f"  - executorId: {task.get('executorId') or ''}")
        lines.append(f"  - dueDate: {task.get('dueDate') or ''}")
        lines.append(f"  - updated: {task.get('updated') or ''}")
    return "\n".join(lines) + "\n"


def write_csv(tasks: list[dict[str, Any]], output: Path) -> None:
    with output.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=["priority", "content", "id", "boardList", "boardListId", "executorId", "dueDate", "updated"],
        )
        writer.writeheader()
        for task in tasks:
            writer.writerow(
                {
                    "priority": task.get("priority", 0),
                    "content": task.get("content") or "",
                    "id": task.get("id") or "",
                    "boardList": task.get("boardListName") or "",
                    "boardListId": task.get("boardListId") or "",
                    "executorId": task.get("executorId") or "",
                    "dueDate": task.get("dueDate") or "",
                    "updated": task.get("updated") or "",
                }
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch unfinished Teambition project tasks.")
    parser.add_argument("--project-id", default="", help="Project id. Defaults to TEAMBITION_PROJECT_ID.")
    parser.add_argument("--list-lists", action="store_true", help="List project board lists and exit.")
    parser.add_argument("--list", action="append", default=[], help="Required board list name. Use --list-lists to discover names.")
    parser.add_argument("--list-id", action="append", default=[], help="Required board list id. Can be used multiple times.")
    parser.add_argument("--min-priority", type=int, default=None)
    parser.add_argument("--include-archived", action="store_true")
    parser.add_argument("--format", choices=("markdown", "json", "csv"), default="markdown")
    parser.add_argument("--output", default="", help="Output file path. Prints to stdout when omitted.")
    parser.add_argument("--move-nth-from-list", default="", help="Move the Nth task from this board list.")
    parser.add_argument("--nth", type=int, default=0, help="1-based task index for --move-nth-from-list.")
    parser.add_argument("--to-list", default="", help="Destination board list name for move operations.")
    return parser.parse_args()


def main() -> int:
    configure_stdio()
    args = parse_args()
    client = TeambitionClient(build_config(args.project_id))
    name_to_board_list, id_to_board_list, ordered_board_list_ids = get_board_list_maps(client)
    if args.list_lists:
        for name, list_id in name_to_board_list.items():
            print(f"{name}\t{list_id}")
        print(f"count={len(name_to_board_list)}")
        return 0
    if args.move_nth_from_list:
        if args.nth < 1:
            raise SystemExit("--move-nth-from-list requires --nth >= 1")
        if not args.to_list:
            raise SystemExit("--move-nth-from-list requires --to-list")
        source_ids = resolve_board_list_ids(name_to_board_list, [args.move_nth_from_list], [])
        target_ids = resolve_board_list_ids(name_to_board_list, [args.to_list], [])
        target_id = next(iter(target_ids))
        source_tasks = filter_tasks(
            client.query_project_tasks(),
            required_board_list_ids=source_ids,
            min_priority=args.min_priority,
            include_archived=args.include_archived,
        )
        if args.nth > len(source_tasks):
            raise SystemExit(f"List {args.move_nth_from_list!r} has only {len(source_tasks)} task(s).")
        task = source_tasks[args.nth - 1]
        task_id = str(task.get("id") or "")
        if not task_id:
            raise SystemExit(f"Selected task has no id: {task!r}")
        client.move_task(task, target_id)
        moved = next((item for item in client.query_project_tasks() if str(item.get("id") or "") == task_id), dict(task))
        moved = annotate_tasks([moved], id_to_board_list)[0]
        print(json.dumps(moved, ensure_ascii=False, indent=2))
        return 0
    required_board_list_ids = resolve_board_list_ids(name_to_board_list, args.list, args.list_id)
    tasks = filter_tasks(
        client.query_project_tasks(),
        required_board_list_ids=required_board_list_ids,
        min_priority=args.min_priority,
        include_archived=args.include_archived,
    )
    tasks = annotate_tasks(tasks, id_to_board_list)

    if args.format == "json":
        content = json.dumps(tasks, ensure_ascii=False, indent=2)
    elif args.format == "csv":
        if not args.output:
            raise SystemExit("--format csv requires --output")
        write_csv(tasks, Path(args.output))
        print(f"Wrote {len(tasks)} tasks to {args.output}")
        return 0
    else:
        content = format_markdown(tasks, ordered_board_list_ids)

    if args.output:
        Path(args.output).write_text(content, encoding="utf-8")
        print(f"Wrote {len(tasks)} tasks to {args.output}")
    else:
        sys.stdout.write(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
