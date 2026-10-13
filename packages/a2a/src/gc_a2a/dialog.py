"""Правила диалога: новая задача, продолжение вопроса, чужой корень."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from gc_a2a.params import Incoming
from gc_a2a.store import (
    INPUT_REQUIRED,
    TERMINAL,
    WORKING,
    TaskStore,
)


class DialogError(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True)
class OpenNew:
    cwd: str


@dataclass(frozen=True)
class OpenTask:
    context_id: str
    cwd: str


@dataclass(frozen=True)
class Resume:
    task_id: str
    context_id: str


def decide(
    store: TaskStore, incoming: Incoming, launch_root: Any
) -> OpenNew | OpenTask | Resume:
    if incoming.task_id is not None:
        return _resume(store, incoming)
    if incoming.context_id is not None:
        return _next(store, incoming)
    return OpenNew(cwd_for_new(incoming.cwd_sent, launch_root))


def _resume(store: TaskStore, incoming: Incoming) -> Resume:
    task = store.task(incoming.task_id or "")
    if task is None:
        raise DialogError("not_found")
    if incoming.context_id not in (None, task.context_id):
        raise DialogError("invalid")
    if task.state in TERMINAL or task.state == WORKING:
        raise DialogError("unsupported")
    if task.state != INPUT_REQUIRED:
        raise DialogError("unsupported")
    dialog = store.dialog(task.context_id)
    if dialog is None:
        raise DialogError("invalid")
    _same_cwd(incoming.cwd_sent, dialog.cwd)
    return Resume(task.id, task.context_id)


def _next(store: TaskStore, incoming: Incoming) -> OpenTask:
    context_id = incoming.context_id or ""
    dialog = store.dialog(context_id)
    if dialog is None:
        raise DialogError("invalid")
    live = _open_tasks(store, dialog.task_ids)
    if any(task.state == WORKING for task in live):
        raise DialogError("unsupported")
    if any(task.state == INPUT_REQUIRED for task in live):
        raise DialogError("invalid")
    _same_cwd(incoming.cwd_sent, dialog.cwd)
    return OpenTask(dialog.context_id, dialog.cwd)


def _open_tasks(store: TaskStore, task_ids: list[str]) -> list:
    live = []
    for task_id in task_ids:
        task = store.task(task_id)
        if task is not None and task.state not in TERMINAL:
            live.append(task)
    return live


def cwd_for_new(sent: str | None, launch_root: Any) -> str:
    if sent is not None:
        if not os.path.isabs(sent):
            raise DialogError("invalid")
        return os.path.normpath(sent)
    if isinstance(launch_root, str) and os.path.isabs(launch_root):
        return os.path.normpath(launch_root)
    return os.path.normpath(os.getcwd())


def _same_cwd(sent: str | None, recorded: str) -> None:
    if sent is None:
        return
    if os.path.normpath(sent) != os.path.normpath(recorded):
        raise DialogError("invalid")
