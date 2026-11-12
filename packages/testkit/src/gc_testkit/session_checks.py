"""Сессия агента (``ThreadSession``) поверх хранилища тредов — путь, которым
ходит ход: шаг, чужие секции рядом, сжатие, повторное открытие.

Конформанс контракта (``thread_store_conformance_v2``) проверяет сами
операции хранилища; здесь — что ход агента поверх них не теряет ни своего
состояния, ни чужих полей. Набор общий для всех хранилищ: каждый пакет
хранилищ зовёт его со своей фабрикой.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from gc_core.thread_store import ThreadSession
from gc_core.threads.facade import Threads
from gc_core.threads.models import SessionState, ThreadKey

KEY = ThreadKey(system="cli", thread="session-check")


async def _open(store: Any) -> ThreadSession:
    return await ThreadSession.afrom_config(
        settings=None,
        system_id=KEY.system,
        thread_id=KEY.thread,
        store=store,
        claim=True,
    )


def _msg(i: str) -> dict:
    return {"id": i, "role": "user", "content": f"текст {i}"}


async def _step(ts: ThreadSession, *messages: dict, **parts: Any) -> None:
    await ts.apersist_batch(
        list(messages),
        previous_response_id=parts.get("previous_response_id"),
        session=parts.get("session", ts.state),
        usage=ts.usage,
    )


async def check_foreign_fields_survive(store: Any) -> None:
    """Посреди хода фронтенд переименовал тред и записал свои секции —
    после шага и сжатия всё на месте."""
    ts = await _open(store)
    await _step(ts, _msg("m1"))
    front = Threads(store)
    await front.update_frontend(KEY, "tui", {"title": "Мой тред", "pin": 1})
    await front.update_frontend(KEY, "webhook", {"status": "running"})
    await _step(ts, _msg("m2"))
    await ts.aarchive_and_compact_summary_payload(
        {"summary_message_content": "сводка"}
    )
    await _step(ts, _msg("m3"))
    record = await store.get_thread(KEY)
    assert record.frontend["tui"]["pin"] == 1, record.frontend
    assert record.frontend["webhook"] == {"status": "running"}
    title = record.info.title or record.frontend["tui"].get("title")
    assert title == "Мой тред", record.info


async def check_session_reopens(store: Any) -> None:
    """Шаг, сжатие, отпущенная аренда — следующая сессия читает сводку и
    состояние хода и продолжает нумерацию истории."""
    ts = await _open(store)
    await _step(
        ts,
        _msg("m1"),
        _msg("m2"),
        session=SessionState(notes="заметки", inbox_seq=2),
        previous_response_id="r1",
    )
    await ts.aarchive_and_compact_summary_payload(
        {"summary_message_content": "сводка"}
    )
    await ts.arelease_lease()
    again = await _open(store)
    assert len(again.history_messages) == 1, again.history_messages
    assert again.persisted_messages_count == 1
    assert again.state.notes == "заметки" and again.state.inbox_seq == 2
    assert again.state.last_summarized_at
    await _step(again, _msg("m3"))
    assert again.persisted_messages_count == 2
    await again.arelease_lease()


CHECKS: tuple[Callable[[Any], Awaitable[None]], ...] = (
    check_foreign_fields_survive,
    check_session_reopens,
)


async def aassert_session_works(make_store: Callable[[], Any]) -> list[str]:
    """Каждая проверка — на свежем хранилище ``make_store()``; хранилище с
    ``close`` закрывается. Возвращает имена пройденных проверок."""
    passed = []
    for check in CHECKS:
        store = make_store()
        try:
            await check(store)
        finally:
            close = getattr(store, "close", None)
            if close is not None:
                await close()
        passed.append(check.__name__)
    return passed
