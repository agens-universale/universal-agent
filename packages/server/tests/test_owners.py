"""Реестр владельцев: пульс инстанса и его задачи «в работе» на тредах.

Тред-реестр на запуск процесса: его аренда — пульс, его секция — задачи;
тред-каталог — список реестров. Восстановление читает каталог и записи
мёртвых, а не все треды (спека docs/superpowers/specs/
2026-10-26-a2a-owner-registry-design.md, 4.1)."""

import asyncio
from datetime import timedelta

import pytest
from gc_core.execution.environment import utc_now
from gc_core.threads.facade import Threads
from gc_core.threads.rules import lease_expired
from gc_server import owners
from gc_server.config import RESERVED_SYSTEMS
from gc_testkit.memory_thread_store_v2 import MemoryThreadStore

TTL = timedelta(seconds=0.3)
RENEW = 0.1


def _registry(threads, **kwargs) -> owners.OwnerRegistry:
    return owners.OwnerRegistry(
        threads, section="a2a", ttl=TTL, renew_seconds=RENEW, **kwargs
    )


@pytest.fixture()
def threads():
    return Threads(MemoryThreadStore())


async def _crash(registry: owners.OwnerRegistry) -> None:
    """Процесс умер: продление остановилось, аренда не отпущена."""
    registry._keeper.cancel()
    await asyncio.sleep(TTL.total_seconds() + 0.1)


async def _directory(threads) -> dict:
    """Реестры каталога (вычеркнутые — ``None`` — не в счёт)."""
    record = await threads.get(owners.DIRECTORY)
    return owners.entries(record, owners.DIRECTORY_SECTION)


async def test_registry_appears_in_the_directory_with_a_live_lease(threads):
    registry = _registry(threads)
    key = await registry.aadd("ctx|t1", {"ctx": "ctx", "task": "t1"})
    try:
        assert key in await _directory(threads)
        record = await threads.get(owners.registry_key(key))
        assert not lease_expired(record.lease, utc_now())
        assert owners.entries(record, "a2a") == {
            "ctx|t1": {"ctx": "ctx", "task": "t1"}
        }
        await registry.aremove("ctx|t1")
        record = await threads.get(owners.registry_key(key))
        assert owners.entries(record, "a2a") == {}
    finally:
        await registry.astop()


async def test_lease_is_renewed_while_the_process_lives(threads):
    registry = _registry(threads)
    key = await registry.aensure()
    try:
        await asyncio.sleep(1.0)
        assert await owners.adead(threads, own=None, now=utc_now()) == []
        record = await threads.get(owners.registry_key(key))
        assert not lease_expired(record.lease, utc_now())
    finally:
        await registry.astop()


async def test_stopped_registry_is_dead_at_once(threads):
    registry = _registry(threads)
    key = await registry.aensure()
    await registry.astop()
    assert await owners.adead(threads, own=None, now=utc_now()) == [key]


async def test_crashed_registry_is_dead_after_ttl_but_not_for_itself(threads):
    registry = _registry(threads)
    key = await registry.aensure()
    await _crash(registry)
    assert await owners.adead(threads, own=None, now=utc_now()) == [key]
    assert await owners.adead(threads, own=key, now=utc_now()) == []


async def test_dead_registry_is_taken_by_one_recoverer(threads):
    registry = _registry(threads)
    key = await registry.aensure()
    await _crash(registry)
    first, second = await asyncio.gather(
        owners.aclaim_dead(threads, key, owner="recover-a", ttl=TTL),
        owners.aclaim_dead(threads, key, owner="recover-b", ttl=TTL),
    )
    assert [r is not None for r in (first, second)].count(True) == 1


async def test_registry_taken_by_a_recoverer_is_lost_by_its_owner(threads):
    """Зомби: продление отстало, реестр взял восстановитель — владелец
    узнаёт о потере (``on_lost``) и заводит новый реестр."""
    lost = asyncio.Event()

    async def on_lost() -> None:
        lost.set()

    registry = _registry(threads, on_lost=on_lost)
    key = await registry.aensure()
    await _crash(registry)
    assert await owners.aclaim_dead(threads, key, owner="recover", ttl=TTL)
    await registry._arenew()
    assert lost.is_set()
    assert registry.key is None
    again = await registry.aadd("ctx|t2", {"ctx": "ctx", "task": "t2"})
    try:
        assert again != key
    finally:
        await registry.astop()


async def test_dropped_registry_is_lost_by_its_owner(threads):
    """Восстановитель уже разобрал и удалил реестр: запоздалое продление
    не воскрешает его как свой (поколение треда сменилось)."""
    lost = asyncio.Event()

    async def on_lost() -> None:
        lost.set()

    registry = _registry(threads, on_lost=on_lost)
    key = await registry.aensure()
    await _crash(registry)
    taken = await owners.aclaim_dead(threads, key, owner="recover", ttl=TTL)
    assert taken is not None
    await owners.adrop(threads, key)
    await registry._arenew()
    assert lost.is_set()
    assert registry.key is None


async def test_directory_key_without_a_registry_is_forgotten(threads):
    await threads.update_frontend(
        owners.DIRECTORY, owners.DIRECTORY_SECTION, {"ghost-1": {}}
    )
    assert await owners.adead(threads, own=None, now=utc_now()) == []
    assert "ghost-1" not in await _directory(threads)


async def test_drop_deletes_the_registry_and_its_directory_key(threads):
    registry = _registry(threads)
    key = await registry.aensure()
    await registry.astop()
    assert await owners.aclaim_dead(threads, key, owner="recover", ttl=TTL)
    await owners.adrop(threads, key)
    assert await threads.get(owners.registry_key(key)) is None
    assert key not in await _directory(threads)


async def test_forget_removes_only_resolved_entries(threads):
    registry = _registry(threads)
    key = await registry.aadd("c|a", {"ctx": "c", "task": "a"})
    await registry.aadd("c|b", {"ctx": "c", "task": "b"})
    await registry.astop()
    await owners.aforget(threads, key, section="a2a", entry_ids=["c|a"])
    record = await threads.get(owners.registry_key(key))
    assert list(owners.entries(record, "a2a")) == ["c|b"]


def test_registry_key_is_a_valid_thread_id():
    key = owners.new_key("host.example.com")
    assert owners.registry_key(key).thread == key
    assert "." not in key


def test_owners_system_is_reserved():
    assert owners.SYSTEM in RESERVED_SYSTEMS


async def _raw_section(threads, key, section) -> dict:
    record = await threads.get(key)
    return dict(record.frontend.get(section) or {})


async def test_registry_section_does_not_grow_with_finished_tasks(threads):
    """Снятая запись — null в секции (контракт слияния); долгоживущий
    процесс с тысячами задач не раздувает свой реестр."""
    registry = _registry(threads)
    try:
        await registry.aadd("c|keep", {"ctx": "c", "task": "keep"})
        for n in range(owners.COMPACT_AFTER * 3):
            await registry.aadd(f"c|t{n}", {"ctx": "c", "task": f"t{n}"})
            await registry.aremove(f"c|t{n}")
        raw = await _raw_section(
            threads, owners.registry_key(registry.key), "a2a"
        )
        assert len(raw) <= owners.COMPACT_AFTER + 1
        assert raw["c|keep"] == {"ctx": "c", "task": "keep"}
    finally:
        await registry.astop()


async def test_compaction_cannot_erase_a_concurrent_add(threads, monkeypatch):
    registry = owners.OwnerRegistry(threads, section="a2a")
    entered, resume = asyncio.Event(), asyncio.Event()
    original = threads.store.append_side

    async def delayed(key, change):
        if change.set_frontend is not None:
            entered.set()
            await resume.wait()
        return await original(key, change)

    key = await registry.aadd("old", {"task": "old"})
    registry._removed = owners.COMPACT_AFTER - 1
    monkeypatch.setattr(threads.store, "append_side", delayed)
    compact = asyncio.create_task(registry.aremove("old"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await registry.aadd("new", {"task": "new"})
        resume.set()
        await compact
        persisted = owners.entries(
            await threads.get(owners.registry_key(key)), "a2a"
        )
        assert persisted == {"new": {"task": "new"}}
    finally:
        resume.set()
        await registry.astop()


async def test_directory_is_compacted_and_live_owners_reassert(threads):
    """Каталог копит вычеркнутые ключи (по одному на запуск процесса):
    восстановитель переписывает его, живой реестр вписывает себя заново."""
    gone = {f"gone-{n}": None for n in range(owners.DIRECTORY_COMPACT_AT + 1)}
    await threads.update_frontend(
        owners.DIRECTORY, owners.DIRECTORY_SECTION, gone
    )
    registry = _registry(threads)
    key = await registry.aensure()
    try:
        await owners.adead(threads, own=None, now=utc_now())
        raw = await _raw_section(
            threads, owners.DIRECTORY, owners.DIRECTORY_SECTION
        )
        assert list(raw) == [key]
        # Гонка с переписыванием: запись живого реестра пропала — он
        # вписывает себя снова на очередном продлении.
        await threads.update_frontend(
            owners.DIRECTORY, owners.DIRECTORY_SECTION, {key: None}
        )
        for _ in range(owners.REASSERT_EVERY + 1):
            await registry._arenew()
        assert key in await _directory(threads)
    finally:
        await registry.astop()


class _FlakyThreads:
    """Фасад тредов, у которого запись в каталог один раз падает
    сетевой ошибкой."""

    def __init__(self, threads) -> None:
        self._threads = threads
        self.failures = 1

    def __getattr__(self, name):
        return getattr(self._threads, name)

    async def update_frontend(self, key, name, patch, **kwargs):
        if key == owners.DIRECTORY and self.failures:
            self.failures -= 1
            raise ConnectionError("сеть моргнула")
        return await self._threads.update_frontend(key, name, patch, **kwargs)


async def test_transient_error_does_not_stop_the_heartbeat(threads):
    """Ревью, C2: сбой повторной записи в каталог убивал продление — живой
    процесс через срок выглядел мёртвым."""
    registry = _registry(threads)
    key = await registry.aensure()
    flaky = _FlakyThreads(threads)
    registry._threads = flaky
    registry._renewals = owners.REASSERT_EVERY - 1
    try:
        await asyncio.sleep(1.0)
        assert flaky.failures == 0
        assert registry.key == key
        assert await owners.adead(threads, own=None, now=utc_now()) == []
    finally:
        await registry.astop()


async def test_add_after_a_stall_does_not_revive_a_dropped_registry(threads):
    """Ревью, I1a: процесс завис дольше срока, его реестр разобрали и
    удалили; новая запись не воскрешает удалённый реестр вне каталога —
    продление вскрывает потерю, запись ложится в новый реестр."""
    registry = _registry(threads)
    key = await registry.aensure()
    await _crash(registry)
    assert await owners.aclaim_dead(threads, key, owner="recover", ttl=TTL)
    await owners.adrop(threads, key)
    again = await registry.aadd("c|b", {"ctx": "c", "task": "b"})
    try:
        assert again != key
        assert await threads.get(owners.registry_key(key)) is None
        assert again in await _directory(threads)
    finally:
        await registry.astop()


async def test_lease_lapsed_without_a_recoverer_is_still_a_loss(threads):
    """Ревью, I1d: аренда истекла (её мог взять и отпустить восстановитель,
    разобрав часть записей) — продление со сменой эпохи — потеря."""
    lost = asyncio.Event()

    async def on_lost() -> None:
        lost.set()

    registry = _registry(threads, on_lost=on_lost)
    await registry.aensure()
    await _crash(registry)
    await registry._arenew()
    assert lost.is_set()
    assert registry.key is None


async def test_one_heartbeat_per_registry_after_a_loss(threads):
    """Ревью, M1: продление прежнего ключа не продлевает новый реестр."""

    async def on_lost() -> None:
        await registry.aensure()

    registry = _registry(threads, on_lost=on_lost)
    await registry.aensure()
    old = registry._keeper
    await _crash(registry)
    old_task = asyncio.create_task(registry._akeep(registry.key))
    await asyncio.sleep(0.3)
    try:
        assert old_task.done()
        assert registry._keeper is not None
        assert not registry._keeper.done()
    finally:
        old_task.cancel()
        del old
        await registry.astop()


async def test_failed_directory_write_does_not_leave_an_orphan(threads):
    """Ревью, M5: реестр взят, а запись в каталог упала — аренда отпущена,
    повтор заводит реестр заново (а не держит невидимый)."""
    registry = _registry(threads)
    registry._threads = _FlakyThreads(threads)
    with pytest.raises(ConnectionError):
        await registry.aensure()
    assert registry.key is None
    key = await registry.aensure()
    try:
        assert key in await _directory(threads)
        held = [
            record.key.thread
            async for record in threads.iter_threads(system=owners.SYSTEM)
            if record.lease is not None
            and not lease_expired(record.lease, utc_now())
        ]
        assert held == [key]
    finally:
        await registry.astop()
