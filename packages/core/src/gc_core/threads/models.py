"""Модели хранилища тредов v2 — данные без ввода-вывода.

Модели — это API между ядром и хранилищем, а не формат хранения: файловое
хранилище раскладывает их в прежние ключи meta develop. Поэтому то, чего модель
не знает, не теряется: неизвестные ключи meta лежат в ``ThreadRecord.extra``,
неизвестные ключи ``runtime`` и ``token_usage`` — в ``extra`` их моделей.

Спека: ``docs/superpowers/specs/2026-10-14-agent-storage-v2-design.md``, 5.1–5.4.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta
from typing import Any, Generic, Literal, TypeVar, get_args

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
)

from gc_core.thread_paths import sanitize_system_id, validate_thread_id

JsonObject = dict[str, Any]

CoreStream = Literal["messages", "subagent_events", "edits", "inbox"]
CORE_STREAMS: frozenset[str] = frozenset(get_args(CoreStream))
RunFlag = Literal["summarizing", "finalizing", "finish_gate_checking"]
TurnState = Literal["idle", "running", "interrupted"]

#: Поток фронтенда: ``<фронтенд>.<имя>``, например ``chat.responses``.
_FRONTEND_STREAM = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
_HISTORY_STREAM = "messages"


def check_stream_name(name: str) -> str:
    """Имя потока ядра или ``<фронтенд>.<имя>``; иначе ``ValueError``."""
    if name in CORE_STREAMS or _FRONTEND_STREAM.match(name):
        return name
    raise ValueError(
        f"неизвестный поток {name!r}: ожидается один из "
        f"{sorted(CORE_STREAMS)} или '<фронтенд>.<имя>'"
    )


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _OpenModel(BaseModel):
    """Модель, сохраняющая ключи, которых не знает (данные develop)."""

    model_config = ConfigDict(frozen=True, extra="allow")


class ThreadKey(_Model):
    """Адрес треда: внешняя система и идентификатор треда в ней."""

    system: str
    thread: str

    @field_validator("system")
    @classmethod
    def _system(cls, value: str) -> str:
        return sanitize_system_id(value)

    @field_validator("thread")
    @classmethod
    def _thread(cls, value: str) -> str:
        return validate_thread_id(value)


class ThreadInfo(_Model):
    """Карточка треда. ``created_at``, ``updated_at`` и ``messages_count``
    ставит хранилище: значения в ``set_info`` для них игнорируются."""

    title: str | None = None
    workspace: str | None = None
    api_mode: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    messages_count: int = 0


class SessionState(_Model):
    """Состояние сессии агента, переживающее ходы."""

    working_memory: dict[str, str] = Field(default_factory=dict)
    notes: str = ""
    #: Пункты ``session_todo`` как есть: смысл задач проверяет ядро, не хранилище.
    todo: list[JsonObject] = Field(default_factory=list)
    fetch_url_cache: JsonObject = Field(default_factory=dict)
    confirm_rules: JsonObject = Field(default_factory=dict)
    intent_log: list[JsonObject] = Field(default_factory=list)
    current_intent: list[JsonObject] = Field(default_factory=list)
    user_turn_seq: int = 0
    dialog_steps_offset: int = 0
    todo_reminder_count: int = 0
    #: Сколько раз показана подсказка об инструменте — по имени инструмента.
    tool_usage_notice: dict[str, int] = Field(default_factory=dict)
    project_memory_version: str | None = None
    project_memory_check_count: int | None = None
    #: Цепочка Responses API (``previous_response_id``) и id последнего
    #: ответа бэкенда — состояние протокола, а не карточка треда.
    previous_response_id: str | None = None
    last_backend_response_id: str | None = None
    last_prompt_tokens: int | None = None
    last_prompt_message_count: int | None = None
    rendered_system_prompt: str | None = None
    last_summarized_at: str | None = None


class Usage(_OpenModel):
    """Расход токенов по категориям вызовов (``UsageAccount.to_meta``)."""

    by_category: dict[str, JsonObject] = Field(default_factory=dict)


class RuntimeStatus(_OpenModel):
    """Живой статус хода: пишется без ревизии треда (``SideChange``)."""

    turn: TurnState | None = None
    flags: frozenset[RunFlag] = frozenset()
    live_msg_count: int | None = None
    live_history_chars: int | None = None
    #: Сквозной номер плашек картинок треда (``[Изображение #N]``).
    image_seq: int | None = None
    mcp_tools: Any = None
    trace_ids: list[str] | None = None

    @field_serializer("flags")
    def _sorted_flags(self, flags: frozenset[str]) -> list[str]:
        # Порядок обхода frozenset зависит от PYTHONHASHSEED: без сортировки
        # отпечаток операции (change_digest) разный в разных процессах, и
        # повтор на другом инстансе получил бы OperationMismatch.
        return sorted(flags)


class LeaseRequest(_Model):
    """Просьба об аренде: ``ttl=None`` — бессрочно (владелец — workflow)."""

    owner: str = Field(min_length=1)
    ttl: timedelta | None = None


class Lease(_Model):
    """Право писать в тред. ``epoch`` растёт при каждой смене владельца."""

    owner: str = Field(min_length=1)
    epoch: int = Field(ge=1)
    expires_at: AwareDatetime | None = None


class StreamEntry(_Model):
    seq: int = Field(ge=1)
    ts: str
    data: JsonObject


class ArchiveInfo(_Model):
    id: str
    created_at: str
    messages_count: int = Field(ge=0)


class BlobRef(_Model):
    """Вложение треда. ``location`` — как ссылка записывается в сообщение:
    путь к файлу у файлового хранилища, ``thread-file://…`` у остальных."""

    key: ThreadKey
    name: str
    mime: str
    location: str


class Compaction(_Model):
    """Новая история вместо старой; ``archive=True`` — старую в архив."""

    messages: list[JsonObject]
    archive: bool = True


class ThreadChange(_Model):
    """Запись истории и состояния: проверяет ревизию, аренду и ``op_id``."""

    op_id: str = Field(min_length=1)
    expected_revision: int = Field(ge=0)
    lease: Lease
    append_messages: list[JsonObject] = Field(default_factory=list)
    set_state: SessionState | None = None
    set_usage: Usage | None = None
    set_info: ThreadInfo | None = None
    #: Шаг пишет и живой объём истории, и счётчик картинок — как develop.
    set_runtime: RuntimeStatus | None = None
    #: Ключи meta, которых модели не знают: форма meta открытая (секции
    #: сторонних процессов отдаются наружу дословно). Заменяет целиком.
    set_extra: JsonObject | None = None
    replace_history: Compaction | None = None
    discard_message_ids: list[str] = Field(default_factory=list)
    reset_history: bool = False
    add_aliases: list[str] = Field(default_factory=list)


class SideChange(_Model):
    """Запись, не конфликтующая с ходом: только ``op_id``, ревизия не растёт.

    История (поток ``messages``) сюда не входит — её пишет только ``commit``.
    """

    op_id: str = Field(min_length=1)
    #: Аренда пишущего, если она у него есть. Запись хода (статус, потоки,
    #: алиасы) в тред с бессрочной арендой (durable-исполнитель) проходит
    #: только по ней: чужой процесс не перепишет статус живой сессии.
    lease: Lease | None = None
    set_runtime: RuntimeStatus | None = None
    set_frontend: tuple[str, JsonObject] | None = None
    #: Слить ключи в состояние фронтенда (``dict.update``; нет — создать):
    #: мелкие правки из разных потоков не теряют друг друга, слияние — под
    #: блокировкой хранилища.
    patch_frontend: tuple[str, JsonObject] | None = None
    append: list[tuple[str, list[JsonObject]]] = Field(default_factory=list)
    add_aliases: list[str] = Field(default_factory=list)

    @field_validator("append")
    @classmethod
    def _streams(
        cls, value: list[tuple[str, list[JsonObject]]]
    ) -> list[tuple[str, list[JsonObject]]]:
        for stream, _entries in value:
            if check_stream_name(stream) == _HISTORY_STREAM:
                raise ValueError("историю треда пишет только commit")
        return value


class Receipt(_Model):
    """Квитанция операции: повтор того же ``op_id`` возвращает её же."""

    op_id: str
    revision: int
    generation: int
    history_version: int
    max_seq: dict[str, int] = Field(default_factory=dict)
    archive_id: str | None = None
    lease: Lease | None = None


class ThreadRecord(_Model):
    """Тред без содержимого потоков: то, что хранилище отдаёт одним чтением."""

    key: ThreadKey
    info: ThreadInfo = Field(default_factory=ThreadInfo)
    state: SessionState = Field(default_factory=SessionState)
    usage: Usage = Field(default_factory=Usage)
    runtime: RuntimeStatus = Field(default_factory=RuntimeStatus)
    frontend: dict[str, JsonObject] = Field(default_factory=dict)
    archives: list[ArchiveInfo] = Field(default_factory=list)
    revision: int = 0
    generation: int = 0
    history_version: int = 0
    max_seq: dict[str, int] = Field(default_factory=dict)
    lease: Lease | None = None
    extra: JsonObject = Field(default_factory=dict)


class ThreadQuery(_Model):
    """Фильтр списка тредов; порядок — ``updated_at`` по убыванию."""

    system: str | None = None
    workspace: str | None = None
    updated_before: str | None = None
    updated_after: str | None = None
    cursor: str | None = None
    limit: int = Field(default=100, ge=1, le=1000)

    @field_validator("system")
    @classmethod
    def _system(cls, value: str | None) -> str | None:
        return None if value is None else sanitize_system_id(value)


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    model_config = ConfigDict(frozen=True)

    items: list[T]
    next_cursor: str | None = None


def change_digest(change: ThreadChange | SideChange) -> str:
    """Отпечаток содержимого операции без аренды.

    Повтор после продления аренды (другой ``expires_at``) — тот же запрос, а не
    ``OperationMismatch``.
    """
    payload = change.model_dump(mode="json", exclude={"lease"})
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
