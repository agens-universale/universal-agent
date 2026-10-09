"""Конформанс durable-хранилища тредов: проверка чужой реализации одной функцией.

Durable-контракт нужен исполнителю, у которого одна и та же запись может
прийти дважды — например, Temporal повторяет activity после падения worker'а.
Обычный локальный режим его не использует. Реализацию для своей базы или S3
встраивающий проверяет так::

    from gc_testkit.durable_thread_store_conformance import (
        assert_durable_conforms,
    )

    def test_my_postgres_store():
        assert_durable_conforms(lambda: PostgresThreadStore(dsn=TEST_DSN))

Контракт — два метода поверх обычного ``ThreadStore``:

* ``read_snapshot(system_id, thread_id, *, include_state=True)`` →
  ``ThreadStoreSnapshot`` (meta, сообщения, revision, generation, владелец);
* ``commit_operation(system_id, thread_id, operation)`` → ``ThreadStoreReceipt``:
  атомарно применить ``ThreadStoreOperation``.

Свойства, которые проверяются ниже:

1. повтор операции с тем же ``operation_id`` и тем же входом возвращает
   исходную квитанцию и ничего не пишет второй раз;
2. тот же ``operation_id`` с другим входом отклоняется;
3. устаревший ``expected_revision`` отклоняется без изменений;
4. владелец треда: чужой писатель получает отказ, освобождает только владелец;
5. ``reset`` продвигает ``generation``, операции старого поколения отклоняются;
6. сессия ядра (``ThreadSession``) пишет, читает и компактирует тред через
   этот контракт так же, как через обычное хранилище.

Свойства 1–5 проверяются на операциях, которые ядро реально отправляет
(``claim_owner``, ``release_owner``, ``save_meta``, ``reset``); формат
пошаговой записи не зашит в проверки — её отправляет настоящая сессия.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, List

from gc_core.thread_store_durable import (
    ThreadStoreConflict,
    ThreadStoreOperation,
    ThreadStoreOperationMismatch,
    ThreadStoreOwnershipError,
)

from gc_testkit.thread_store_conformance import run_checks

SYS = "cli"
THREAD = "durable"


class DurableThreadStoreContractError(AssertionError):
    """Реализация не удовлетворяет durable-контракту. Текст называет, чем именно."""


def _fail(what: str, detail: str) -> None:
    raise DurableThreadStoreContractError(f"{what}\n  {detail}")


def _op(operation_id, kind, payload=None, **fields) -> ThreadStoreOperation:
    return ThreadStoreOperation(operation_id, kind, payload or {}, **fields)


def _expect(error_type, what, call) -> None:
    try:
        call()
    except error_type:
        return
    except Exception as exc:
        _fail(what, f"ожидали {error_type.__name__}, получили {exc!r}")
    _fail(what, f"ожидали {error_type.__name__}, ошибки не было")


def check_methods_present(store: Any) -> None:
    for name in ("read_snapshot", "commit_operation"):
        if not callable(getattr(store, name, None)):
            _fail("Нет метода durable-контракта", name)


def check_empty_snapshot(store: Any) -> None:
    snapshot = store.read_snapshot(SYS, THREAD)
    if snapshot.state is None or snapshot.state.messages:
        _fail(
            "Пустой тред",
            f"ожидали пустую историю, получили {snapshot.state!r}",
        )
    if snapshot.owner_id is not None:
        _fail(
            "Пустой тред", f"владелец должен быть None: {snapshot.owner_id!r}"
        )
    meta_only = store.read_snapshot(SYS, THREAD, include_state=False)
    if meta_only.state is not None:
        _fail("include_state=False", "состояние истории не должно читаться")


def check_retry_returns_original_receipt(store: Any) -> None:
    operation = _op("meta-1", "save_meta", {"meta": {"note": "first"}})
    first = store.commit_operation(SYS, THREAD, operation)
    revision = store.read_snapshot(SYS, THREAD).revision
    second = store.commit_operation(SYS, THREAD, operation)
    if second != first:
        _fail(
            "Повтор операции", f"квитанции различаются: {first!r} / {second!r}"
        )
    if store.read_snapshot(SYS, THREAD).revision != revision:
        _fail("Повтор операции", "повтор не должен менять revision")


def check_reused_operation_id_with_other_input(store: Any) -> None:
    store.commit_operation(
        SYS, THREAD, _op("meta-1", "save_meta", {"meta": {"note": "first"}})
    )
    _expect(
        ThreadStoreOperationMismatch,
        "Тот же operation_id с другим входом",
        lambda: store.commit_operation(
            SYS,
            THREAD,
            _op("meta-1", "save_meta", {"meta": {"note": "second"}}),
        ),
    )


def check_stale_revision_rejected(store: Any) -> None:
    before = store.read_snapshot(SYS, THREAD)
    store.commit_operation(
        SYS, THREAD, _op("meta-1", "save_meta", {"meta": {"note": "first"}})
    )
    _expect(
        ThreadStoreConflict,
        "Устаревший expected_revision",
        lambda: store.commit_operation(
            SYS,
            THREAD,
            _op(
                "meta-2",
                "save_meta",
                {"meta": {"note": "stale"}},
                expected_revision=before.revision,
            ),
        ),
    )
    meta = store.read_snapshot(SYS, THREAD).meta
    if meta.get("note") != "first":
        _fail("Устаревший expected_revision", f"meta изменилась: {meta!r}")


def check_owner_fence(store: Any) -> None:
    store.commit_operation(
        SYS, THREAD, _op("claim-a", "claim_owner", {"owner_id": "a"})
    )
    snapshot = store.read_snapshot(SYS, THREAD)
    if snapshot.owner_id != "a":
        _fail("Захват треда", f"владелец {snapshot.owner_id!r} вместо 'a'")
    _expect(
        ThreadStoreOwnershipError,
        "Захват занятого треда другим владельцем",
        lambda: store.commit_operation(
            SYS, THREAD, _op("claim-b", "claim_owner", {"owner_id": "b"})
        ),
    )
    _expect(
        ThreadStoreOwnershipError,
        "Запись не от владельца",
        lambda: store.commit_operation(
            SYS,
            THREAD,
            _op("meta-b", "save_meta", {"meta": {}}, owner_id="b"),
        ),
    )
    store.commit_operation(
        SYS,
        THREAD,
        _op("meta-a", "save_meta", {"meta": {"by": "a"}}, owner_id="a"),
    )
    store.commit_operation(
        SYS, THREAD, _op("release-a", "release_owner", {}, owner_id="a")
    )
    store.commit_operation(
        SYS, THREAD, _op("claim-b2", "claim_owner", {"owner_id": "b"})
    )
    if store.read_snapshot(SYS, THREAD).owner_id != "b":
        _fail(
            "Освобождение треда",
            "после release новый владелец не захватил тред",
        )


def check_reset_advances_generation(store: Any) -> None:
    store.commit_operation(
        SYS, THREAD, _op("meta-1", "save_meta", {"meta": {"note": "first"}})
    )
    before = store.read_snapshot(SYS, THREAD)
    store.commit_operation(
        SYS,
        THREAD,
        _op("reset-1", "reset", {}, generation=before.generation),
    )
    after = store.read_snapshot(SYS, THREAD)
    if after.generation <= before.generation:
        _fail(
            "reset",
            f"generation не выросла: {before.generation} → {after.generation}",
        )
    _expect(
        ThreadStoreOwnershipError,
        "Операция старого поколения после reset",
        lambda: store.commit_operation(
            SYS,
            THREAD,
            _op(
                "meta-old",
                "save_meta",
                {"meta": {}},
                generation=before.generation,
            ),
        ),
    )


def check_session_roundtrip_and_compaction(store: Any) -> None:
    from gc_core.settings import AppConfig
    from gc_core.thread_store import ThreadSession

    async def scenario():
        session = await ThreadSession.afrom_config(
            settings=AppConfig(),
            system_id=SYS,
            thread_id=THREAD,
            store=store,
        )
        first = [{"id": "m1", "role": "user", "content": "first"}]
        second = [{"id": "m2", "role": "assistant", "content": "second"}]
        await session.apersist_batch(first, previous_response_id=None)
        await session.apersist_batch(first + second, previous_response_id=None)
        reloaded = await ThreadSession.afrom_config(
            settings=AppConfig(),
            system_id=SYS,
            thread_id=THREAD,
            store=store,
        )
        ids = [m.get("id") for m in reloaded.history_messages]
        if ids != ["m1", "m2"]:
            _fail(
                "Запись через ThreadSession",
                f"история {ids!r}, ожидали ['m1', 'm2']",
            )
        if reloaded.meta.get("messages_count") != 2:
            _fail(
                "Запись через ThreadSession",
                f"messages_count={reloaded.meta.get('messages_count')!r}",
            )
        await reloaded.aarchive_and_compact_summary_payload(
            {"summary_message_content": "compressed", "dialog_steps_offset": 1}
        )
        compacted = store.read_snapshot(SYS, THREAD)
        if len(compacted.state.messages) != 1:
            _fail(
                "Компакция через ThreadSession",
                f"после компакции {len(compacted.state.messages)} сообщений",
            )

    asyncio.run(scenario())


STRUCTURE_CHECKS = (check_methods_present,)
BEHAVIOUR_CHECKS = (
    check_empty_snapshot,
    check_retry_returns_original_receipt,
    check_reused_operation_id_with_other_input,
    check_stale_revision_rejected,
    check_owner_fence,
    check_reset_advances_generation,
    check_session_roundtrip_and_compaction,
)


def assert_durable_conforms(store_factory: Callable[[], Any]) -> List[str]:
    """Проверить durable-хранилище целиком; фабрика вызывается перед каждой проверкой."""
    return run_checks(store_factory, STRUCTURE_CHECKS, BEHAVIOUR_CHECKS)


__all__ = [
    "BEHAVIOUR_CHECKS",
    "STRUCTURE_CHECKS",
    "DurableThreadStoreContractError",
    "assert_durable_conforms",
]
