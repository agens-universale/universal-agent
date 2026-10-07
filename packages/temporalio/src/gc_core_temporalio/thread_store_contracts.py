"""Values passed to the configured native agent ThreadStore."""

from dataclasses import dataclass
from typing import Any

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
