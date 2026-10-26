"""Values passed to the configured native agent ThreadStore."""

from dataclasses import dataclass
from typing import Any

from gc_core.execution.contracts import ExecutionFailure
from gc_core.thread_store_durable import (
    ThreadStoreOperation,
    ThreadStoreReceipt,
    ThreadStoreSnapshot,
)

#: Потолок одной страницы чтения треда/трассы (байты JSON). Результат
#: activity — запись истории Temporal (лимит 2 МБ, предупреждение сервера с
#: 512 КБ): длинный тред читается несколькими activities, каждая ниже порога
#: предупреждения.
PAGE_BYTES = 400 * 1024


class MetaBaseMismatch(ExecutionFailure):
    """Воркер не собрал из разницы ту meta, что записал workflow (база в
    хранилище другая: чужая правка meta, повтор после записи). Workflow
    повторяет запись с полной meta."""


@dataclass
class ThreadStoreCall:
    store_id: str
    system_id: str
    thread_id: str
    operation: ThreadStoreOperation | None = None
    include_state: bool = True
    #: Страничное чтение: ``page_bytes`` > 0 — отдать кусок не больше
    #: стольких байт (не меньше одной записи) с позиции ``cursor``.
    page_bytes: int = 0
    cursor: list[int] | None = None
    #: Следующие страницы: ``[revision, generation]`` снимка первой — если
    #: тред изменился между страницами, отказ ``ThreadStoreConflict``.
    expect: list[int] | None = None
    #: Чтение meta частями: позиция в JSON-тексте meta (см. ``meta_chunk``).
    meta_cursor: int | None = None
    #: Чтение: ``[revision, generation]`` meta, которую workflow уже знает —
    #: совпала с хранилищем, meta не передаётся (``meta_unchanged``).
    meta_known: list[Any] | None = None
    #: Запись: ``[revision, generation]`` meta, от которой посчитана разница;
    #: ``None`` — workflow meta треда не знает (разница от пустой).
    meta_base: list[Any] | None = None
    #: Запись с meta: ``{"d": разница, "h": хэш итоговой meta}``
    #: (``meta_delta.diff_meta``/``meta_hash``), а в
    #: ``operation.payload["meta"]`` — ``None``; воркер собирает полную meta
    #: до вызова хранилища и сверяет хэш (не сошлось — ``MetaBaseMismatch``).
    meta_delta: dict[str, Any] | None = None
    #: ``False`` — meta квитанции не нужна (трасса, владение без базы):
    #: воркер не считает разницу и не просит перечитать meta.
    need_meta: bool = True


@dataclass
class ThreadStatePage:
    """Кусок состояния треда: сообщения, затем ключи идемпотентности."""

    messages: list[dict[str, Any]]
    keys: list[Any]
    #: Позиция следующей страницы ``[сообщение, ключ]``; ``None`` — конец.
    next_cursor: list[int] | None = None
    #: ``ThreadState.max_seq`` (номер последнего seq) — в первой странице.
    max_seq: int = 0


@dataclass
class ThreadStoreReply:
    snapshot: ThreadStoreSnapshot | None = None
    receipt: ThreadStoreReceipt | None = None
    trace: list[dict[str, Any]] | None = None
    page: ThreadStatePage | None = None
    #: Страничное чтение трассы: позиция следующей записи; ``None`` — конец.
    trace_next: int | None = None
    #: Meta больше половины страницы: в ``snapshot.meta`` пусто, здесь кусок
    #: её JSON-текста, ``meta_next`` — позиция следующего (``None`` — конец).
    meta_chunk: str | None = None
    meta_next: int | None = None
    #: Квитанция приходит с пустой ``meta``: ``{"d": разница}`` от meta,
    #: которую workflow ожидает после операции (записанной им или базы).
    meta_delta: dict[str, Any] | None = None
    #: Воркер не знает, какую meta ожидает workflow (повтор после записи,
    #: другая база) или разница велика: meta перечитать.
    meta_reload: bool = False
    #: Чтение: meta та же, что у workflow (``meta_known``) — не передана.
    meta_unchanged: bool = False
