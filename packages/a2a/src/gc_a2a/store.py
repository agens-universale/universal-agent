"""Задачи и диалоги этого процесса. Источник состояния фасада."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Callable

from gc_a2a.errors import SealError

WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
FAILED = "TASK_STATE_FAILED"
CANCELED = "TASK_STATE_CANCELED"
TERMINAL = frozenset({COMPLETED, FAILED, CANCELED, "TASK_STATE_REJECTED"})


def utc_stamp() -> str:
    now = datetime.now(UTC)
    millis = now.microsecond // 1000
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{millis:03d}Z"


@dataclass
class Dialog:
    context_id: str
    cwd: str
    task_ids: list[str] = field(default_factory=list)


@dataclass
class Task:
    id: str
    context_id: str
    state: str
    timestamp: str
    history: list[dict]
    sequence: int
    artifact_id: str | None = None
    artifact_text: str | None = None
    status_message: dict | None = None
    broken: bool = False


class TaskStore:
    def __init__(self, clock: Callable[[], str] | None = None) -> None:
        self._clock = clock or utc_stamp
        self._cv = asyncio.Condition()
        self._tasks: dict[str, Task] = {}
        self._dialogs: dict[str, Dialog] = {}
        self._messages: dict[str, str] = {}
        self._sequence = 0

    def message_task(self, message_id: str) -> str | None:
        return self._messages.get(message_id)

    def dialog(self, context_id: str) -> Dialog | None:
        return self._dialogs.get(context_id)

    def task(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def dialogs(self) -> dict[str, Dialog]:
        return self._dialogs

    def tasks(self) -> dict[str, Task]:
        return self._tasks

    async def create_task(
        self,
        *,
        task_id: str,
        context_id: str,
        cwd: str,
        message_id: str,
        parts: tuple[str, ...],
        new_dialog: bool,
    ) -> None:
        async with self._cv:
            if new_dialog:
                self._dialogs[context_id] = Dialog(context_id, cwd)
            dialog = self._dialogs[context_id]
            dialog.task_ids.append(task_id)
            self._put(
                task_id,
                context_id,
                [_user_message(message_id, parts)],
                message_id,
            )

    async def accept_reply(
        self, task_id: str, message_id: str, parts: tuple[str, ...]
    ) -> None:
        async with self._cv:
            task = self._tasks[task_id]
            task.history.append(_user_message(message_id, parts))
            self._messages[message_id] = task_id
            self._become(task, WORKING, status_message=None)

    async def mark_input_required(
        self, task_id: str, message_id: str, question: str
    ) -> None:
        async with self._cv:
            task = self._tasks[task_id]
            if task.state in TERMINAL or task.broken:
                return
            message = _agent_message(message_id, question)
            task.history.append(message)
            self._become(task, INPUT_REQUIRED, status_message=message)

    async def mark_completed(
        self, task_id: str, text: str, artifact_id: str
    ) -> None:
        async with self._cv:
            task = self._tasks[task_id]
            task.artifact_id = artifact_id
            task.artifact_text = text
            self._become(task, COMPLETED, status_message=None)

    async def mark_failed(
        self, task_id: str, message_id: str, text: str
    ) -> None:
        async with self._cv:
            task = self._tasks[task_id]
            self._become(
                task,
                FAILED,
                status_message=_agent_message(message_id, text),
            )

    async def mark_canceled(self, task_id: str) -> None:
        async with self._cv:
            task = self._tasks[task_id]
            self._become(task, CANCELED, status_message=None)

    async def mark_broken(self, task_id: str) -> None:
        async with self._cv:
            task = self._tasks.get(task_id)
            if task is None:
                raise SealError()
            task.broken = True
            self._cv.notify_all()

    async def wait_view(
        self, task_id: str, history_length: int | None
    ) -> dict:
        async with self._cv:
            task = self._tasks[task_id]
            while task.state == WORKING and not task.broken:
                await self._cv.wait()
            if task.broken and task.state == WORKING:
                raise SealError()
            return public(task, history_length, include_artifacts=True)

    async def view(
        self,
        task_id: str,
        history_length: int | None,
        *,
        include_artifacts: bool = True,
    ) -> dict:
        async with self._cv:
            return public(
                self._tasks[task_id],
                history_length,
                include_artifacts=include_artifacts,
            )

    def view_now(self, task_id: str, history_length: int | None) -> dict:
        return public(
            self._tasks[task_id],
            history_length,
            include_artifacts=True,
        )

    async def snapshot(self) -> list[Task]:
        async with self._cv:
            return [
                replace(task, history=list(task.history))
                for task in self._tasks.values()
            ]

    def _put(
        self,
        task_id: str,
        context_id: str,
        history: list[dict],
        message_id: str,
    ) -> None:
        self._sequence += 1
        task = Task(
            id=task_id,
            context_id=context_id,
            state=WORKING,
            timestamp=self._clock(),
            history=history,
            sequence=self._sequence,
        )
        self._tasks[task_id] = task
        self._messages[message_id] = task_id
        self._cv.notify_all()

    def _become(
        self, task: Task, state: str, *, status_message: dict | None
    ) -> None:
        task.state = state
        task.timestamp = self._clock()
        task.status_message = status_message
        self._cv.notify_all()


def public(
    task: Task, history_length: int | None, *, include_artifacts: bool
) -> dict:
    status: dict[str, Any] = {
        "state": task.state,
        "timestamp": task.timestamp,
    }
    if task.status_message is not None:
        status["message"] = task.status_message
    body: dict[str, Any] = {
        "id": task.id,
        "contextId": task.context_id,
        "status": status,
    }
    if include_artifacts and task.artifact_text is not None:
        body["artifacts"] = [
            {
                "artifactId": task.artifact_id,
                "parts": [
                    {"text": task.artifact_text, "mediaType": "text/plain"}
                ],
            }
        ]
    _history(body, task.history, history_length)
    return body


def _history(
    body: dict, history: list[dict], history_length: int | None
) -> None:
    if history_length is None:
        body["history"] = list(history)
    elif history_length > 0:
        body["history"] = list(history[-history_length:])


def _user_message(message_id: str, parts: tuple[str, ...]) -> dict:
    return {
        "messageId": message_id,
        "role": "ROLE_USER",
        "parts": [{"text": part} for part in parts],
    }


def _agent_message(message_id: str, text: str) -> dict:
    return {
        "messageId": message_id,
        "role": "ROLE_AGENT",
        "parts": [{"text": text, "mediaType": "text/plain"}],
    }
