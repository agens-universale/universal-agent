"""Задачи A2A и настройки push — в хранилище тредов, видны с любого инстанса.

Задача — записи потока ``a2a.tasks`` треда диалога (``system="a2a"``, тред
— ``contextId``): снимок на каждое изменение. Состояние фронтенда ``a2a``
держит малый индекс ``task:<id>`` → ``seq`` последнего снимка (а также
очередь диалога и его корень — ``executor``); ``taskId`` находит тред
алиасом ``a2a-task:<id>``. Настройки push задачи приходят раньше самой
задачи (в ``SendMessage``), поэтому лежат в своём треде ``push-<taskId>``.
"""

from __future__ import annotations

import uuid
from typing import Any

from a2a.server.context import ServerCallContext
from a2a.server.tasks import TaskStore
from a2a.server.tasks.push_notification_config_store import (
    PushNotificationConfigStore,
    normalize_push_notification_config,
)
from a2a.types import a2a_pb2 as pb
from a2a.utils.constants import DEFAULT_LIST_TASKS_PAGE_SIZE
from a2a.utils.errors import InvalidParamsError
from a2a.utils.task import (
    ListTasksCursor,
    decode_list_tasks_cursor,
    encode_list_tasks_cursor,
)
from gc_core.threads.facade import Threads
from gc_core.threads.models import SideChange, ThreadKey, ThreadRecord
from google.protobuf.json_format import MessageToDict, ParseDict

SYSTEM = "a2a"
FRONTEND = "a2a"
#: Снимки задач диалога.
TASKS_STREAM = "a2a.tasks"
_PUSH = "a2a_push"
TASK_PREFIX = "task:"
#: Ключи очереди диалога в состоянии фронтенда ``a2a`` (ведёт её
#: ``executor``; отмену, записанную мимо исполнителя, учитывает ``save``).
#: Задача, чей ход идёт: ``{task, instance, at}``.
RUNNING = "running"
#: Задача, ждущая ответа на вопрос (держит очередь диалога).
WAITING = "waiting"
#: Очередь диалога: ``q:<taskId>`` → ``{at, seen, instance}``.
QUEUE = "q:"
#: Отменённая задача: ``x:<taskId>`` → ``true`` (в очередь не встанет).
CANCELED = "x:"


def task_alias(task_id: str) -> str:
    return f"a2a-task:{task_id}"


def dialog_key(context_id: str) -> ThreadKey:
    return ThreadKey(system=SYSTEM, thread=context_id)


def _op(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def dialog_state(record: ThreadRecord | None) -> dict[str, Any]:
    """Состояние фронтенда ``a2a`` треда диалога (копия)."""
    return dict((record.frontend.get(FRONTEND) or {}) if record else {})


def task_index(record: ThreadRecord | None) -> dict[str, Any]:
    """Индекс задач диалога: ``task_id`` → ``seq`` последнего снимка."""
    return {
        name[len(TASK_PREFIX) :]: value
        for name, value in dialog_state(record).items()
        if name.startswith(TASK_PREFIX) and value is not None
    }


class ThreadTaskStore(TaskStore):
    """``TaskStore`` SDK поверх хранилища тредов."""

    def __init__(self, threads: Threads) -> None:
        self._threads = threads

    async def save(self, task: pb.Task, context: ServerCallContext) -> None:
        del context
        key = dialog_key(task.context_id)
        receipt = await self._threads.store.append_side(
            key,
            SideChange(
                op_id=_op("a2a_task"),
                append=[(TASKS_STREAM, [MessageToDict(task)])],
                add_aliases=[task_alias(task.id)],
            ),
        )
        # Индекс — отдельной записью: номер снимка известен после записи.
        # Упади процесс между ними — индекс укажет прежний снимок задачи.
        patch: dict[str, Any] = {
            TASK_PREFIX + task.id: receipt.max_seq.get(TASKS_STREAM)
        }
        if task.status.state == pb.TASK_STATE_CANCELED:
            patch.update(await self._arelease(key, task.id))
        await self._threads.update_frontend(key, FRONTEND, patch)

    async def _arelease(self, key: ThreadKey, task_id: str) -> dict[str, Any]:
        """Отменённая задача уходит из очереди диалога. SDK пишет отмену
        и сам, мимо исполнителя (задача не на этом инстансе, рестарт):
        ждущая ответа задача освобождает диалог, ожидающая очереди — с неё
        снимается, а идущий где-то ход получает отмену через поток
        ``control`` треда."""
        state = dialog_state(await self._threads.get(key))
        patch: dict[str, Any] = {
            QUEUE + task_id: None,
            CANCELED + task_id: True,
        }
        if state.get(WAITING) == task_id:
            patch[WAITING] = None
        running = state.get(RUNNING)
        if isinstance(running, dict) and running.get("task") == task_id:
            await self._threads.request_cancel(key, target=task_id)
        return patch

    async def get(
        self, task_id: str, context: ServerCallContext
    ) -> pb.Task | None:
        del context
        return await self.latest(task_id)

    async def latest(self, task_id: str) -> pb.Task | None:
        """Последний снимок задачи."""
        key = await self._threads.resolve(task_alias(task_id))
        if key is None:
            return None
        record = await self._threads.get(key)
        return await self.at(key, task_index(record).get(task_id))

    async def at(self, key: ThreadKey, position: Any) -> pb.Task | None:
        """Снимок задачи по записи индекса (одно чтение потока)."""
        if not isinstance(position, int) or position < 1:
            return None
        entries = await self._threads.store.read_stream(
            key, TASKS_STREAM, after_seq=position - 1, limit=1
        )
        if not entries or entries[0].seq != position:
            return None
        return _parse(entries[0].data)

    async def list(
        self, params: pb.ListTasksRequest, context: ServerCallContext
    ) -> pb.ListTasksResponse:
        del context
        tasks = [
            task
            for task in await self._tasks(params.context_id or None)
            if _matches(task, params)
        ]
        # Порядок и курсор страниц — как у хранилища SDK в памяти.
        tasks.sort(key=_sort_key, reverse=True)
        start = _start(tasks, params.page_token)
        size = params.page_size or DEFAULT_LIST_TASKS_PAGE_SIZE
        page = tasks[start : start + size]
        more = start + size < len(tasks)
        return pb.ListTasksResponse(
            next_page_token=(
                encode_list_tasks_cursor(_cursor(page[-1])) if more else None
            ),
            tasks=page,
            page_size=size,
            total_size=len(tasks),
        )

    async def _tasks(self, context_id: str | None) -> list[pb.Task]:
        """Последние снимки задач: обход индексов диалогов."""
        if context_id is not None:
            record = await self._threads.get(dialog_key(context_id))
            records = [record] if record is not None else []
        else:
            records = [
                record
                async for record in self._threads.iter_threads(system=SYSTEM)
            ]
        tasks = []
        for record in records:
            for position in task_index(record).values():
                task = await self.at(record.key, position)
                if task is not None:
                    tasks.append(task)
        return tasks

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        del context
        key = await self._threads.resolve(task_alias(task_id))
        if key is not None:
            await self._threads.update_frontend(
                key, FRONTEND, {TASK_PREFIX + task_id: None}
            )


def _matches(task: pb.Task, params: pb.ListTasksRequest) -> bool:
    if params.status and task.status.state != params.status:
        return False
    if params.HasField("status_timestamp_after"):
        after = params.status_timestamp_after.ToNanoseconds()
        stamped = task.HasField("status") and task.status.HasField("timestamp")
        if not stamped or task.status.timestamp.ToNanoseconds() < after:
            return False
    return True


def _sort_key(task: pb.Task) -> tuple[bool, int, str]:
    stamped = task.HasField("status") and task.status.HasField("timestamp")
    return (
        stamped,
        task.status.timestamp.ToNanoseconds() if stamped else 0,
        task.id,
    )


def _cursor(task: pb.Task) -> ListTasksCursor:
    stamped, timestamp_ns, task_id = _sort_key(task)
    return ListTasksCursor(
        timestamp_ns=timestamp_ns if stamped else None, task_id=task_id
    )


def _start(tasks: list[pb.Task], page_token: str) -> int:
    if not page_token:
        return 0
    cursor = decode_list_tasks_cursor(page_token)
    if cursor is None:
        raise InvalidParamsError(message="page_token: битый курсор страницы")
    after = cursor.sort_key()
    return next(
        (i for i, task in enumerate(tasks) if _sort_key(task) < after),
        len(tasks),
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
