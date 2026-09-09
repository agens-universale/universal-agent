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
import inspect

import pytest

from gc_core.thread_store import ThreadSession
from gc_core.thread_store_async import (
    ASYNC_CONTRACT_METHODS,
    SLOW_CALL_WARN_SEC,
    AsyncThreadStore,
    SyncThreadStoreAdapter,
    ThreadStoreContractError,
    athread_store,
    is_async_thread_store,
)
from gc_core.thread_store_file import FileThreadStore
from gc_core.thread_store_base import CONTRACT_METHODS
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

    def _make(name):  # noqa: N805 — фабрика методов, self появляется в замыкании
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


async def test_sync_and_async_paths_agree(tmp_path):
    """Синхронный и асинхронный пути дают одно и то же.

    Главное свойство разделения: у них различается ТОЛЬКО последовательность
    обращений к хранилищу, вся логика — в общих чистых помощниках. Разъехаться
    им негде, и этот тест краснеет, если кто-то заведёт вторую копию логики.
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

    sync_store = MemoryThreadStore()
    s = ThreadSession.from_config(
        config_dict={}, system_id=SYS, thread_id=TID, store=sync_store,
    )
    s.persist_batch(messages[:2], previous_response_id="r1")
    s.persist_batch(messages[2:], previous_response_id="r2")
    sync_result = s.archive_and_compact_summary_payload(payload)

    async_store = MemoryThreadStore()
    a = await ThreadSession.afrom_config(
        config_dict={}, system_id=SYS, thread_id=TID, store=async_store,
    )
    await a.apersist_batch(messages[:2], previous_response_id="r1")
    await a.apersist_batch(messages[2:], previous_response_id="r2")
    async_result = await a.aarchive_and_compact_summary_payload(payload)

    assert sync_result["new_messages_count"] == async_result["new_messages_count"]
    assert (
        [m["content"] for m in sync_store.load_messages(SYS, TID)]
        == [m["content"] for m in async_store.load_messages(SYS, TID)]
    )
    sync_meta = {k: v for k, v in sync_store.load_meta(SYS, TID).items() if k not in volatile}
    async_meta = {k: v for k, v in async_store.load_meta(SYS, TID).items() if k not in volatile}
    assert sync_meta == async_meta


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


def test_sync_sidecar_path_warns_instead_of_dropping(tmp_path, caplog):
    """Синхронный путь sidecar не может ждать — но и молчать не должен.

    Сюда приходит сообщение живому субагенту из чужого потока
    (``ActiveChildScope.enqueue``), и ждать там нечем. С асинхронным
    хранилищем записать трассу отсюда нельзя, но молчание было бы худшим из
    исходов: корутина выбрасывалась бы, а встраивающий видел бы работающих
    субагентов с пустой историей вызовов.
    """
    import logging
    from types import SimpleNamespace

    from gc_core.tools.subagents.sidecar import persist_subagent_trace

    ctx = SimpleNamespace(
        parent_call_id="call-1", threads_dir=str(tmp_path),
        system_id=SYS, thread_id=TID, run_id="run-1",
        thread_store=type(
            "AsyncOnly", (), {"append_subagent_trace": _coroutine_stub()},
        )(),
    )
    with caplog.at_level(logging.WARNING, logger="gc_core.thread_store"):
        assert persist_subagent_trace(ctx, kind="call", payload={}) is None
    assert "subagent_trace_skipped_async_store" in caplog.text


def _coroutine_stub():
    async def method(self, *args, **kwargs):
        return None
    return method


# --- предупреждение о блокирующем хранилище --------------------------------

async def test_adapter_warns_on_slow_sync_store():
    """Синхронное СЕТЕВОЕ хранилище предупреждает о себе один раз.

    Отличить его от быстрого локального статически нельзя, а разница велика:
    максимальный лаг цикла ≈ ``11 × RTT + 29 мс`` (25 обращений за прогон
    укладываются в три блока подряд — 11, 11 и 3). При 10 мс на вызов бюджет
    приёмки в 150 мс уже пробит.
    """
    class SlowStore(MemoryThreadStore):
        def load_meta(self, system_id, thread_id):
            import time
            time.sleep(SLOW_CALL_WARN_SEC * 3)
            return super().load_meta(system_id, thread_id)

    from gc_core.thread_store_async import SLOW_CALL_PROBE_COUNT

    warnings: list = []
    port = athread_store(SlowStore(), warn=warnings.append)
    # Суждение выносится по МЕДИАНЕ окна замеров, а не по отдельному вызову:
    # иначе единичный всплеск на обычных файлах советовал бы их владельцу
    # переписать хранилище на асинхронное (поймано на живом стенде).
    for _ in range(SLOW_CALL_PROBE_COUNT + 2):
        await port.aload_meta(SYS, TID)

    assert len(warnings) == 1, "предупреждение должно быть однократным"
    assert "thread_store_blocking_loop" in warnings[0]
    assert "load_meta" in warnings[0]
    assert "AsyncThreadStore" in warnings[0], "не подсказан асинхронный контракт"


async def test_adapter_is_quiet_when_only_one_call_spikes():
    """Единичный всплеск на быстром хранилище не поднимает тревогу.

    Ровно этот случай поймал живой стенд: ``FileThreadStore`` с медианой
    0,29 мс дал отдельные вызовы по 2,5–2,8 мс (запись меты, конкуренция за
    диск между агентами), и порог по одному вызову советовал владельцу ОБЫЧНЫХ
    ФАЙЛОВ переписать хранилище на асинхронное. Шумное предупреждение
    перестают читать.
    """
    from gc_core.thread_store_async import SLOW_CALL_PROBE_COUNT

    class SpikyStore(MemoryThreadStore):
        def __init__(self):
            super().__init__()
            self.calls_made = 0

        def load_meta(self, system_id, thread_id):
            import time
            self.calls_made += 1
            if self.calls_made == 1:          # один медленный вызов из всех
                time.sleep(SLOW_CALL_WARN_SEC * 4)
            return super().load_meta(system_id, thread_id)

    warnings: list = []
    port = athread_store(SpikyStore(), warn=warnings.append)
    for _ in range(SLOW_CALL_PROBE_COUNT + 2):
        await port.aload_meta(SYS, TID)

    assert warnings == [], (
        f"единичный всплеск поднял тревогу: {warnings}"
    )


async def test_adapter_is_quiet_on_fast_stores():
    """Быстрое хранилище не предупреждает ни о чём."""
    warnings: list = []
    port = athread_store(MemoryThreadStore(), warn=warnings.append)
    for _ in range(5):
        await port.aload_meta(SYS, TID)
        await port.aappend_messages(SYS, TID, [_msg("m1")])
    assert warnings == []


def test_adapter_covers_every_contract_method():
    """У адаптера есть все одиннадцать ``a``-методов.

    Гейт на недостачу: пропущенный метод обнаружился бы только в рантайме, на
    том пути, который редко исполняется.
    """
    adapter = SyncThreadStoreAdapter(MemoryThreadStore())
    for name in ASYNC_CONTRACT_METHODS:
        assert inspect.iscoroutinefunction(getattr(adapter, name, None)), name
