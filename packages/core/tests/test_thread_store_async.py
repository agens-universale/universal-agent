"""Гейт: хранилище с асинхронным клиентом работает, и работает целиком.

Задача. Контракт хранилища синхронный — правильно по умолчанию: файлы и
локальная база отвечают за микросекунды. Но встраивающий с ``redis.asyncio``
или ``asyncpg`` синхронного клиента не держит вовсе, и до этих правок ядро его
реализацию вызвать не могло. Хуже: вызывало молча неправильно.

Что именно ломалось. Двенадцать из шестнадцати обращений ядра к хранилищу
лежали в обычных ``def``, где корутина отбрасывается БЕЗ ОШИБКИ и БЕЗ ЛОГА.
Падали только два места, остальные молчали:

* индикаторы TUI (``summarizing`` / ``finalizing`` / ``finish_gate_checking``)
  не загорались никогда;
* ``token_usage`` после ручной ``/compact`` не сохранялся — ``or {}`` пропускал
  корутину (она истинна), а ``TypeError`` съедал ``except Exception: pass``;
* ``meta.runtime.trace_ids`` не писался, ошибка уходила в ``logger.debug``;
* снимок ``mcp_tools`` не писался — экран ``/mcp`` пуст при работающих серверах;
* трасса субагентов не писалась ни разу: возврат отбрасывают все семь
  вызывающих, поэтому исключения не было ни в одном.

То есть встраивающий получал работающего агента с пустой историей субагентов,
неверными токенами и мёртвыми индикаторами. Здесь каждое из этих мест
проверяется на живом асинхронном хранилище.
"""
import asyncio

import pytest
from gc_core.thread_store import ThreadSession
from gc_core.thread_store_async import (
    ASYNC_CONTRACT_METHODS,
    AsyncThreadStore,
    SyncThreadStoreAdapter,
    ThreadStoreContractError,
    athread_store,
    is_async_thread_store,
)
from gc_core.thread_store_base import CONTRACT_METHODS
from gc_core.thread_store_file import FileThreadStore
from gc_testkit.memory_thread_store import MemoryThreadStore
from gc_testkit.thread_store_conformance import assert_conforms_async

SYS, TID = "cli", "t-1"


class RedisLikeStore:
    """Хранилище с асинхронным клиентом: синхронных методов нет вовсе.

    Так выглядит реализация поверх ``redis.asyncio`` или ``asyncpg`` —
    ``await asyncio.sleep(0)`` изображает сетевой round-trip.
    """

    def __init__(self, delay: float = 0.0) -> None:
        self._inner = MemoryThreadStore()
        self._delay = delay
        self.awaited = 0

    def _make(name):
        async def method(self, *args, **kwargs):
            self.awaited += 1
            await asyncio.sleep(self._delay)
            return getattr(self._inner, name)(*args, **kwargs)

        method.__name__ = f"a{name}"
        return method

    for _name in CONTRACT_METHODS:
        locals()[f"a{_name}"] = _make(_name)
    del _make, _name


def _msg(mid: str, text: str = "текст") -> dict:
    return {"id": mid, "role": "user", "content": text}


# --- контракт --------------------------------------------------------------

def test_async_store_passes_the_conformance_suite():
    """Те же двенадцать свойств, что и у синхронной реализации.

    Набор один на обе формы намеренно: свойства (идемпотентность, мутация
    ``known_keys``, ``max_seq`` как номер) от корутинности не зависят, а второй
    набор разошёлся бы с первым.
    """
    from gc_testkit.thread_store_conformance import (
        ASYNC_STRUCTURE_CHECKS,
        BEHAVIOUR_CHECKS,
    )

    passed = assert_conforms_async(RedisLikeStore)
    assert len(passed) == len(ASYNC_STRUCTURE_CHECKS) + len(BEHAVIOUR_CHECKS)


def test_port_passes_native_async_through():
    """Нативная реализация используется как есть, без адаптера."""
    store = RedisLikeStore()
    assert is_async_thread_store(store)
    assert athread_store(store) is store


def test_port_wraps_sync_stores():
    """Синхронные реализации продолжают работать — через адаптер."""
    for store in (MemoryThreadStore(), FileThreadStore("/tmp/none")):
        assert isinstance(athread_store(store), SyncThreadStoreAdapter)


def test_adapter_satisfies_the_async_protocol():
    assert isinstance(athread_store(MemoryThreadStore()), AsyncThreadStore)


def test_half_async_under_sync_names_is_rejected():
    """Смешивать формы нельзя — и отказ обязан сказать почему.

    Самый неприятный случай: часть записей уходит в базу, часть теряется
    молча, и тред расщепляется без единого признака ошибки.
    """
    class Half(MemoryThreadStore):
        async def load_meta(self, system_id, thread_id):  # type: ignore[override]
            return {}

    with pytest.raises(ThreadStoreContractError) as err:
        athread_store(Half())
    assert "load_meta" in str(err.value)
    assert "молча" in str(err.value)


def test_incomplete_async_contract_is_rejected():
    """Половина ``a``-методов — отказ с перечислением недостающих."""
    class Partial:
        pass

    for name in list(ASYNC_CONTRACT_METHODS)[:5]:
        async def method(self, *args, **kwargs):
            return None
        setattr(Partial, name, method)

    with pytest.raises(ThreadStoreContractError) as err:
        athread_store(Partial())
    assert "неполна" in str(err.value)
    assert "areset" in str(err.value)


# --- сквозь сессию ---------------------------------------------------------

async def test_session_reads_and_writes_through_the_async_store():
    store = RedisLikeStore()
    session = await ThreadSession.afrom_config(
        config_dict={}, system_id=SYS, thread_id=TID, store=store,
    )
    assert session.port is store, "порт завернул нативное хранилище в адаптер"

    await session.apersist_batch([_msg("m1", "раз")], previous_response_id=None)
    await session.apersist_batch([_msg("m2", "два")], previous_response_id="r1")

    assert [m["content"] for m in store._inner.load_messages(SYS, TID)] == ["раз", "два"]
    assert store._inner.load_meta(SYS, TID)["messages_count"] == 2
    assert store.awaited > 0, "ни одна корутина не была дождана"


async def test_adapter_and_native_port_agree(tmp_path):
    """Синхронное хранилище через адаптер и асинхронное нативно — одно и то же.

    Сессия теперь ходит в хранилище ЕДИНСТВЕННОЙ формой (синхронные двойники
    ``from_config`` / ``persist_batch`` / ``archive_and_compact_summary_payload``
    сняты), и сравнивать два пути СЕССИИ больше нечего. Осталась настоящая
    двойственность — на уровне ХРАНИЛИЩА: синхронную реализацию ядро видит
    через ``SyncThreadStoreAdapter``, асинхронную — своим портом напрямую.
    Тред обеими дорогами обязан получиться один и тот же.

    Прежняя версия сравнивала синхронный путь сессии с асинхронным. После
    снятия двойников она сравнивала бы два ОДИНАКОВЫХ прогона и зеленела бы
    при любой поломке.
    """
    payload = {
        "summary_message_content": "сводка",
        "dialog_steps_offset": 3,
        "shared": {"working_memory": "wm"},
        "user_intent_log": ["хочу"],
    }
    messages = [_msg(f"m{i}", f"сообщение {i}") for i in range(1, 5)]
    volatile = {"created_at", "updated_at", "last_summarized_at",
                "last_summary_archive_suffix"}

    async def _run(store):
        session = await ThreadSession.afrom_config(
            config_dict={}, system_id=SYS, thread_id=TID, store=store,
        )
        await session.apersist_batch(messages[:2], previous_response_id="r1")
        await session.apersist_batch(messages[2:], previous_response_id="r2")
        return await session.aarchive_and_compact_summary_payload(payload)

    sync_store = MemoryThreadStore()                 # ядро видит через адаптер
    native_store = RedisLikeStore()                  # ядро видит своим портом
    sync_result = await _run(sync_store)
    native_result = await _run(native_store)

    assert native_store.awaited > 0, (
        "проверка пустая: асинхронное хранилище ни разу не позвали нативно"
    )
    assert sync_result["new_messages_count"] == native_result["new_messages_count"]
    assert (
        [m["content"] for m in sync_store.load_messages(SYS, TID)]
        == [m["content"] for m in native_store._inner.load_messages(SYS, TID)]
    )
    sync_meta = {k: v for k, v in sync_store.load_meta(SYS, TID).items() if k not in volatile}
    native_meta = {
        k: v for k, v in native_store._inner.load_meta(SYS, TID).items()
        if k not in volatile
    }
    assert sync_meta == native_meta


async def test_compaction_without_files_does_not_write_to_disk(tmp_path, monkeypatch):
    """Компакция на хранилище без файлов НИЧЕГО не пишет на диск.

    Раньше метод ходил в файлы напрямую: ``_thread_paths(self.threads_dir, …)``
    плюс голый ``open(data_path, "w")``. У хранилища без файлов ``threads_dir``
    — пустая строка, и запись уходила в ОТНОСИТЕЛЬНЫЙ путь от текущего
    каталога. Молча, мимо всего остального треда.
    """
    monkeypatch.chdir(tmp_path)
    store = MemoryThreadStore()
    session = await ThreadSession.afrom_config(
        config_dict={}, system_id=SYS, thread_id=TID, store=store,
    )
    await session.apersist_batch([_msg("m1", "раз")], previous_response_id=None)
    await session.aarchive_and_compact_summary_payload(
        {"summary_message_content": "сводка"}
    )

    created = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert created == [], f"компакция создала файлы мимо хранилища: {created}"
    assert len(store.load_messages(SYS, TID)) == 1, "история не заместилась сводкой"


async def test_file_store_still_archives(tmp_path):
    """У файлового хранилища архив на месте — способность объявлена им."""
    store = FileThreadStore(str(tmp_path))
    store.ensure_ready()
    session = await ThreadSession.afrom_config(
        config_dict={}, system_id=SYS, thread_id=TID, store=store,
    )
    await session.apersist_batch([_msg("m1", "раз")], previous_response_id=None)
    result = await session.aarchive_and_compact_summary_payload(
        {"summary_message_content": "сводка"}
    )
    assert result["archive_data_path"], "архив не создан"
    assert "__pre_summary__" in result["archive_data_path"]


# --- то, что раньше ломалось молча -----------------------------------------

async def test_runtime_flags_reach_the_async_store():
    """Индикаторы TUI доезжают до асинхронного хранилища.

    Самый тихий из прежних отказов: возврат ``patch_runtime_flag`` не
    используется вообще, поэтому корутина просто выбрасывалась — ни ошибки, ни
    лога. Индикаторы «сжимаю контекст» / «финализирую» / «судья работает» не
    загорались никогда.
    """
    from gc_core.run_summary_coordinator import (
        _asummarizing_flag,
        aset_tui_finalizing,
        aset_tui_finish_gate_checking,
    )

    store = RedisLikeStore()
    session = await ThreadSession.afrom_config(
        config_dict={}, system_id=SYS, thread_id=TID, store=store,
    )

    seen = {}
    async with _asummarizing_flag(session):
        seen["during"] = store._inner.load_meta(SYS, TID).get("runtime", {}).get("summarizing")
    assert seen["during"] is True, "флаг не взведён в асинхронном хранилище"
    assert "runtime" not in store._inner.load_meta(SYS, TID), "секция runtime не убрана"

    await aset_tui_finalizing(session, True)
    assert store._inner.load_meta(SYS, TID)["runtime"]["finalizing"] is True
    await aset_tui_finalizing(session, False)

    await aset_tui_finish_gate_checking(session, True)
    assert store._inner.load_meta(SYS, TID)["runtime"]["finish_gate_checking"] is True


async def test_trace_ids_reach_the_async_store():
    """``meta.runtime.trace_ids`` пишется. Прежде ошибка уходила в debug-лог."""
    from gc_core.agent_executor import _aappend_trace_id_to_meta

    store = RedisLikeStore()
    session = await ThreadSession.afrom_config(
        config_dict={}, system_id=SYS, thread_id=TID, store=store,
    )
    await _aappend_trace_id_to_meta(session, "trace-abc", None)
    assert store._inner.load_meta(SYS, TID)["runtime"]["trace_ids"] == ["trace-abc"]

    await _aappend_trace_id_to_meta(session, "trace-abc", None)
    assert store._inner.load_meta(SYS, TID)["runtime"]["trace_ids"] == ["trace-abc"], (
        "повтор задвоил trace_id"
    )


async def test_subagent_trace_reaches_the_async_store(tmp_path):
    """Трасса субагента пишется. Прежде не писалась НИ РАЗУ.

    Возврат отбрасывают все семь вызывающих, поэтому исключения не возникало
    нигде: субагенты работали, а история их вызовов оставалась пустой.
    """
    from types import SimpleNamespace

    from gc_core.tools.subagents.sidecar import apersist_subagent_trace

    store = RedisLikeStore()
    ctx = SimpleNamespace(
        parent_call_id="call-1", threads_dir=str(tmp_path),
        system_id=SYS, thread_id=TID, run_id="run-1", thread_store=store,
    )
    record = await apersist_subagent_trace(ctx, kind="call", payload={"name": "fs_read"})
    assert record is not None and record["kind"] == "call"
    assert len(store._inner.read_subagent_trace(SYS, TID)) == 1


async def test_compaction_keeps_the_trace_on_a_store_without_replace_messages(caplog):
    """Хранилище без `replace_messages` не теряет трассу субагентов при компакции.

    `replace_messages` в контракт НЕ входит: он объявлен умолчанием базы, а
    `CONTRACT_METHODS` собираются из абстрактных методов. Хранилище, принятое
    «по сигнатурам» (законный путь, см. docs/thread-store.md), этого метода не
    имеет — и компакция шла парой `areset` + `aappend_messages`, где `reset` по
    контракту сносит тред вместе с трассой. Терялась она молча и каждый раз, а
    конформанс оставался зелёным.
    """
    import logging

    store = RedisLikeStore()
    assert not hasattr(store, "areplace_messages"), "двойник перестал быть утиным"
    session = await ThreadSession.afrom_config(
        config_dict={}, system_id=SYS, thread_id=TID, store=store,
    )
    await session.apersist_batch([_msg("m1", "раз")], previous_response_id=None)
    await store.aappend_subagent_trace(
        SYS, TID, parent_call_id="c1", child_run_id="r1",
        kind="call", payload={"task": "проверить"},
    )

    with caplog.at_level(logging.WARNING):
        await session.aarchive_and_compact_summary_payload(
            {"summary_message_content": "сводка"}
        )

    trace = await store.aread_subagent_trace(SYS, TID)
    assert [r["child_run_id"] for r in trace] == ["r1"], f"трасса потеряна: {trace}"
    assert [r["payload"] for r in trace] == [{"task": "проверить"}]
    assert "areplace_messages" in caplog.text, (
        "потеря штампов трассы прошла молча — хранилищу не сказали, что делать"
    )
