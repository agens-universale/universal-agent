"""Задачи A2A и настройки push — в хранилище тредов, видны с любого инстанса.

Задача лежит в состоянии фронтенда ``a2a`` треда диалога (``system="a2a"``,
тред — ``contextId``) под ключом ``task:<id>``; ``taskId`` находит тред
алиасом ``a2a-task:<id>``. Настройки push задачи приходят раньше самой
задачи (в ``SendMessage``), поэтому лежат в своём треде ``push-<taskId>``.
"""

from __future__ import annotations

import uuid
from typing import Any

from a2a.server.context import ServerCallContext
from a2a.server.tasks import InMemoryTaskStore, TaskStore
from a2a.server.tasks.push_notification_config_store import (
    PushNotificationConfigStore,
    normalize_push_notification_config,
)
from a2a.types import a2a_pb2 as pb
from gc_core.threads.facade import Threads
from gc_core.threads.models import SideChange, ThreadKey
from google.protobuf.json_format import MessageToDict, ParseDict

SYSTEM = "a2a"
FRONTEND = "a2a"
_PUSH = "a2a_push"
_TASK_PREFIX = "task:"


def task_alias(task_id: str) -> str:
    return f"a2a-task:{task_id}"


def dialog_key(context_id: str) -> ThreadKey:
    return ThreadKey(system=SYSTEM, thread=context_id)


def _op(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class ThreadTaskStore(TaskStore):
    """``TaskStore`` SDK поверх хранилища тредов."""

    def __init__(self, threads: Threads) -> None:
        self._threads = threads

    async def save(self, task: pb.Task, context: ServerCallContext) -> None:
        del context
        await self._threads.store.append_side(
            dialog_key(task.context_id),
            SideChange(
                op_id=_op("a2a_task"),
                patch_frontend=(
                    FRONTEND,
                    {_TASK_PREFIX + task.id: MessageToDict(task)},
                ),
                add_aliases=[task_alias(task.id)],
            ),
        )

    async def get(
        self, task_id: str, context: ServerCallContext
    ) -> pb.Task | None:
        del context
        key = await self._threads.resolve(task_alias(task_id))
        if key is None:
            return None
        record = await self._threads.get(key)
        state = (record.frontend.get(FRONTEND) or {}) if record else {}
        return _parse(state.get(_TASK_PREFIX + task_id))

    async def list(
        self, params: pb.ListTasksRequest, context: ServerCallContext
    ) -> pb.ListTasksResponse:
        # Фильтры и страницы — как у хранилища SDK в памяти: те же правила
        # порядка и курсора.
        view = InMemoryTaskStore()
        async for record in self._threads.iter_threads(system=SYSTEM):
            state = record.frontend.get(FRONTEND) or {}
            for name, raw in state.items():
                task = _parse(raw) if name.startswith(_TASK_PREFIX) else None
                if task is not None:
                    await view.save(task, context)
        return await view.list(params, context)

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        del context
        key = await self._threads.resolve(task_alias(task_id))
        if key is not None:
            await self._threads.update_frontend(
                key, FRONTEND, {_TASK_PREFIX + task_id: None}
            )


class ThreadPushConfigStore(PushNotificationConfigStore):
    """Настройки push-уведомлений задач — в хранилище тредов."""

    def __init__(self, threads: Threads) -> None:
        self._threads = threads

    async def set_info(
        self,
        task_id: str,
        notification_config: pb.TaskPushNotificationConfig,
        context: ServerCallContext,
    ) -> pb.TaskPushNotificationConfig:
        del context
        config = normalize_push_notification_config(
            task_id, notification_config
        )
        configs = [
            c for c in await self._configs(task_id) if c.id != config.id
        ]
        configs.append(config)
        await self._write(task_id, configs)
        return config

    async def get_info(
        self, task_id: str, context: ServerCallContext
    ) -> list[pb.TaskPushNotificationConfig]:
        del context
        return await self._configs(task_id)

    async def get_info_for_dispatch(
        self, task_id: str
    ) -> list[pb.TaskPushNotificationConfig]:
        return await self._configs(task_id)

    async def delete_info(
        self,
        task_id: str,
        context: ServerCallContext,
        config_id: str | None = None,
    ) -> None:
        del context
        configs = await self._configs(task_id)
        keep = [
            c for c in configs if config_id is not None and c.id != config_id
        ]
        await self._write(task_id, keep)

    async def _configs(self, task_id: str) -> list:
        record = await self._threads.get(_push_key(task_id))
        state = (record.frontend.get(_PUSH) or {}) if record else {}
        return [
            ParseDict(raw, pb.TaskPushNotificationConfig())
            for raw in state.get("configs") or []
        ]

    async def _write(self, task_id: str, configs: list) -> None:
        await self._threads.update_frontend(
            _push_key(task_id),
            _PUSH,
            {"configs": [MessageToDict(c) for c in configs]},
        )


def _push_key(task_id: str) -> ThreadKey:
    return ThreadKey(system=SYSTEM, thread=f"push-{task_id}")


def _parse(raw: Any) -> pb.Task | None:
    if not isinstance(raw, dict):
        return None
    return ParseDict(raw, pb.Task(), ignore_unknown_fields=True)
