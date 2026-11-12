"""Обработчик запросов A2A: сообщение — новой задаче или ответом на вопрос.

SDK принимает сообщение в любую нетерминальную задачу и ставит его ещё
одним запросом той же задачи: повтор ответа, пока ход идёт, сыграл бы её
второй раз. У нас задача — один ход: сообщение с ``taskId`` — только ответ
задаче, которая его ждёт (``INPUT_REQUIRED``); иначе — отказ запросу, сама
задача не меняется.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from a2a.server.context import ServerCallContext
from a2a.server.request_handlers.default_request_handler_v2 import (
    DefaultRequestHandlerV2,
)
from a2a.types import a2a_pb2 as pb
from a2a.utils.errors import InvalidParamsError


class GcRequestHandler(DefaultRequestHandlerV2):
    """``DefaultRequestHandlerV2`` с отказом сообщению в задачу, которая не
    ждёт ответа."""

    async def on_message_send(
        self, params: pb.SendMessageRequest, context: ServerCallContext
    ) -> Any:
        await self._arefuse_unasked(params, context)
        return await super().on_message_send(params, context)

    async def on_message_send_stream(
        self, params: pb.SendMessageRequest, context: ServerCallContext
    ) -> AsyncGenerator[Any, None]:
        await self._arefuse_unasked(params, context)
        async for event in super().on_message_send_stream(params, context):
            yield event

    async def _arefuse_unasked(
        self, params: pb.SendMessageRequest, context: ServerCallContext
    ) -> None:
        task_id = params.message.task_id
        if not task_id:
            return
        task = await self.task_store.get(task_id, context)
        if task is None or task.status.state == pb.TASK_STATE_INPUT_REQUIRED:
            return  # задачи нет — ответит SDK (TaskNotFound)
        state = pb.TaskState.Name(task.status.state)
        raise InvalidParamsError(
            message=f"задача {task_id} не ждёт сообщения: {state}"
        )
