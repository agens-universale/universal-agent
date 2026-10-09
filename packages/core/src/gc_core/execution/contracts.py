"""Versioned values exchanged across an execution boundary (no live handles)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from gc_core.execution.guard_contracts import GuardProbeCheck


class TurnFailure(Exception):
    """Штатный отказ хода: ход завершается ошибкой, как в local.

    Базовый класс доменных отказов (как ``AgentRunError`` у Pydantic AI):
    durable-исполнитель переводит его в отказ run (FAILED). Любое другое
    исключение из кода workflow он считает сбоем кода и повторяет workflow
    task до исправленного воркера.
    """


class ExecutionFailure(TurnFailure, RuntimeError):
    """An execution/persistence failure, never model-visible tool feedback."""


class PayloadTooLarge(ExecutionFailure):
    """Запрос не помещается в транспорт durable-исполнения (запись истории).

    Локально такой запрос уходит провайдеру и отклоняется им; кто
    деградирует на отказе провайдера (сводка при лимите), так же деградирует
    и здесь."""


class InfrastructureFailure(ExecutionFailure):
    """A transport or storage operation could not complete."""


class OutcomeUnknown(ExecutionFailure):
    """An external operation may have completed; do not blindly repeat it."""


class ModelRequestStopped(Exception):
    """Запрос к модели прерван остановкой хода (stop во время ожидания).

    Не ошибка хода: цикл завершает ход как остановленный, так же как при
    проверке stop между шагами. Durable-исполнитель отменяет ожидающую
    activity модели — иначе stop ждал бы конца её повторов.
    """


class ToolOutcomeUnknown(Exception):
    """Исход внешнего эффекта инструмента неизвестен.

    Это ошибка инструмента, а не сбой хода: модель получает её как обычный
    вывод инструмента и сама проверяет состояние, прежде чем повторять.
    """


@dataclass
class StateDelta:
    working_memory: dict[str, str] | None = None
    session_todo: list[dict[str, str]] | None = None
    fetch_url_cache: list[list[Any]] | None = None
    #: Снимки, которые тул приложил к хвосту батча (``fs_view_image``):
    #: дописываются к ``state.pending_tool_attachments``.
    pending_tool_attachments: list[dict[str, Any]] | None = None
    schema_version: int = 1


@dataclass
class ArtifactRef:
    digest: str
    size_bytes: int
    media_type: str = "text/plain; charset=utf-8"
    schema_version: int = 1


@dataclass
class ToolResult:
    output: str
    outcome: Literal["ok", "tool_error"] = "ok"
    delta: StateDelta = field(default_factory=StateDelta)
    artifacts: list[ArtifactRef] = field(default_factory=list)
    schema_version: int = 1


@dataclass
class ToolVisionView:
    """Срез State для ``fs_view_image``: зрение хода и картинки шага.

    ``known_attachments`` — вложения истории (словари ``Attachment``): по ним
    тул называет уже живую картинку. Пикселей здесь нет — только ссылки.
    """

    probe_turn: str | None = None
    turn_level: str | None = None
    live_ids: list[str] = field(default_factory=list)
    step_attachments: list[dict[str, Any]] = field(default_factory=list)
    known_attachments: list[dict[str, Any]] = field(default_factory=list)
    view_tool_in_set: bool = False
    #: Сессия запроса (``State.state_id``, ``x_target``): проба зрения из
    #: инструмента на воркере шлёт их в заголовках, как ход.
    state_id: str | None = None
    x_target: str | None = None
    schema_version: int = 1


@dataclass
class ToolStateView:
    """Only state owned by session_memory/session_todo, never a copied State.

    ``vision`` — только для ``fs_view_image`` (см. ``ToolVisionView``).
    """

    working_memory: dict[str, str] = field(default_factory=dict)
    session_todo: list[dict[str, str]] = field(default_factory=list)
    fetch_url_cache: list[list[Any]] = field(default_factory=list)
    vision: ToolVisionView | None = None
    schema_version: int = 1


@dataclass
class ToolEffectRequest:
    operation_id: str
    name: str
    call_id: str | None
    arguments: dict[str, Any]
    arguments_digest: str
    state: ToolStateView = field(default_factory=ToolStateView)
    guard_checks: list[GuardProbeCheck] = field(default_factory=list)
    effect_step: str = "body"
    effect_data: dict[str, Any] = field(default_factory=dict)
    #: Адрес треда хода ``[system_id, thread_id]``: файлы треда (снимки
    #: ``fs_view_image``, медиа MCP) инструмент на воркере кладёт в него.
    thread: list[str] | None = None
    schema_version: int = 1


ToolEffectExecutor = Callable[[ToolEffectRequest], Awaitable[ToolResult]]


class GuardPreconditionChanged(Exception):
    """Guard facts changed between approval and effect; no effect was run.

    Ошибка инструмента: модель видит её и может повторить вызов.
    """


@dataclass
class ModelRequest:
    operation_id: str
    consumer: str
    messages: list[dict[str, Any]]
    state: dict[str, Any]
    max_tokens: int | None = None
    request_label: str = "chat_completion"
    payload_overrides: dict[str, Any] | None = None
    set_previous_response_id: bool = True
    schema_version: int = 1


@dataclass
class ModelFailure:
    """Serializable classification of an exhausted model operation."""

    kind: Literal["domain", "provider", "execution"]
    error_type: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1
    auth_error: bool = False
    retryable: bool = False
    unavailable: bool = False
    context_overflow: bool = False
    response_cache_miss: bool = False
    # Отказ из-за картинок в запросе (``backend_errors.is_image_*``): модель
    # их не принимает вовсе или принимает не все; предел — если назван.
    image_rejection: bool = False
    image_request: bool = False
    image_limit: int | None = None


class ContextWindowExceeded(TurnFailure, RuntimeError):
    """Контекст не помещается в окно модели и после сжатия истории."""


class ModelProviderError(TurnFailure, RuntimeError):
    """Provider failure restored without importing an HTTP client in workflow.

    Native backend-error predicates consume the explicit classification. The
    local execution path still raises the original provider exception.
    """

    def __init__(self, failure: ModelFailure):
        super().__init__(failure.message)
        self.failure = failure


@dataclass
class ModelResult:
    """Canonical model data and usage delta for the calling agent.

    ``messages`` retain the native response items, including reasoning and
    tool calls. ``response_metadata`` contains agent-used response metadata
    (currently x-target), never the provider's raw HTTP headers.
    """

    messages: list[dict[str, Any]] = field(default_factory=list)
    text: str | None = None
    response_id: str | None = None
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    prompt_tokens: int | None = None
    prompt_message_count: int | None = None
    response_metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1
    context_overflow: bool = False
    failure: ModelFailure | None = None
    #: Состояние зрения после запроса основного цикла: какие картинки ушли
    #: модели целиком (``live_ids`` — для ``fs_view_image``) и успешный
    #: потолок картинок (``request_limit``). ``None`` — запрос его не трогал.
    vision: dict[str, Any] | None = None


class ModelOperationError(Exception):
    """Carry native error and partial state across the local model boundary."""

    def __init__(self, cause: Exception, result: ModelResult):
        super().__init__(str(cause))
        self.cause = cause
        self.result = result


class ModelOperationCancelled(asyncio.CancelledError):
    """Cancellation with partial usage; never caught by ``except Exception``."""

    def __init__(self, cause: asyncio.CancelledError, result: ModelResult):
        super().__init__(*cause.args)
        self.cause = cause
        self.result = result
