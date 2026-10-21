"""Прокси одного треда: песочница swarm ходит в хранилище тредов сервера
только своим тредом и только операциями хода — без учётных данных базы.

Сервер поднимает на задачу unix-сокет с токеном; клиент
(``gc_stores.remote``) — обычный ``ThreadStore``, отказы контракта приходят
своим типом."""

import pytest
from gc_core.threads.errors import OwnershipError, ThreadConflict
from gc_core.threads.models import (
    LeaseRequest,
    SideChange,
    ThreadChange,
    ThreadKey,
    ThreadQuery,
)
from gc_stores.remote import open as open_remote
from gc_stores.remote.server import ThreadProxy
from gc_testkit import session_checks
from gc_testkit.memory_thread_store_v2 import MemoryThreadStore

KEY = ThreadKey(system="generic", thread="task1")


async def _pair(store=None, key=KEY):
    proxy = await ThreadProxy.astart(store or MemoryThreadStore(), key)
    client = open_remote({"socket": proxy.socket_path, "token": proxy.token})
    return proxy, client


async def test_session_of_the_task_works_through_the_proxy():
    """Путь хода (сессия агента) через прокси: шаг, чужие секции, сжатие,
    повторное открытие."""
    for check in session_checks.CHECKS:
        proxy, client = await _pair(key=session_checks.KEY)
        try:
            await check(client)
        finally:
            await proxy.aclose()


async def test_contract_errors_arrive_by_type():
    proxy, client = await _pair()
    try:
        await client.claim(KEY, LeaseRequest(owner="sandbox"))
        with pytest.raises(OwnershipError):
            await client.claim(KEY, LeaseRequest(owner="other"))
        record = await client.get_thread(KEY)
        with pytest.raises(ThreadConflict):
            await client.commit(
                KEY,
                ThreadChange(
                    op_id="x",
                    expected_revision=record.revision + 5,
                    lease=record.lease,
                ),
            )
    finally:
        await proxy.aclose()


async def test_other_threads_and_admin_operations_are_forbidden():
    store = MemoryThreadStore()
    other = ThreadKey(system="generic", thread="victim")
    await store.append_side(other, SideChange(op_id="seed"))
    proxy, client = await _pair(store)
    try:
        with pytest.raises(PermissionError):
            await client.get_thread(other)
        with pytest.raises(PermissionError):
            await client.read_stream(other, "messages")
        with pytest.raises(PermissionError):
            await client.list_threads(ThreadQuery())
        with pytest.raises(PermissionError):
            await client.delete(KEY, op_id="d")
        with pytest.raises(PermissionError):
            await client.resolve_alias("resp_1")
        with pytest.raises(PermissionError):
            await client.append_side(
                KEY, SideChange(op_id="a", add_aliases=["resp_1"])
            )
    finally:
        await proxy.aclose()
    assert await store.get_thread(other) is not None


async def test_wrong_token_is_refused():
    proxy, _client = await _pair()
    stranger = open_remote({"socket": proxy.socket_path, "token": "0" * 64})
    try:
        with pytest.raises(PermissionError):
            await stranger.get_thread(KEY)
    finally:
        await proxy.aclose()


async def test_closed_proxy_is_store_unavailable():
    from gc_core.threads.errors import StoreUnavailable

    proxy, client = await _pair()
    await proxy.aclose()
    with pytest.raises(StoreUnavailable):
        await client.get_thread(KEY)


async def test_blobs_of_the_task_only():
    proxy, client = await _pair()
    try:
        ref = await client.put_blob(KEY, b"\x89PNG", mime="image/png")
        assert await client.get_blob(ref) == b"\x89PNG"
        foreign = ref.model_copy(
            update={"key": ThreadKey(system="generic", thread="victim")}
        )
        with pytest.raises(PermissionError):
            await client.get_blob(foreign)
    finally:
        await proxy.aclose()


#: Проверки конформанса, упирающиеся в запрещённое прокси: список и
#: удаление тредов, алиасы, чистка квитанций, чужой тред.
_REFUSED = {
    "check_missing_thread",
    "check_aliases",
    "check_list_threads_filters",
    "check_list_pagination_is_stable",
    "check_list_rejects_broken_cursor",
    "check_delete",
    "check_prune_receipts",
    "check_watch_across_delete",
    "check_epoch_survives_delete",
    "check_alias_collision",
    "check_side_writes_to_durable_thread",
    "check_delete_spares_a_durable_thread",
    "check_concurrent_alias_race",
}


async def test_contract_holds_through_the_proxy():
    """Конформанс v2 через прокси: всё, кроме запрещённого, проходит;
    запрещённое отказывает ``PermissionError``, а не чем-то другим."""
    from gc_testkit import thread_store_conformance_v2 as conformance

    refused = set()
    for check in conformance.CHECKS:
        clock = conformance.FakeClock()
        proxy = await ThreadProxy.astart(
            MemoryThreadStore(clock=clock), conformance.KEY
        )
        client = open_remote(
            {"socket": proxy.socket_path, "token": proxy.token}
        )
        try:
            await check(client, clock)
        except (PermissionError, conformance.ConformanceError) as exc:
            if check.__name__ not in _REFUSED:
                raise
            assert "PermissionError" in repr(exc) or isinstance(
                exc, PermissionError
            ), exc
            refused.add(check.__name__)
        finally:
            await proxy.aclose()
    assert refused == _REFUSED


async def test_sandbox_config_opens_the_proxy(tmp_path, monkeypatch):
    """config.yml песочницы (подстановки окружения, фабрика
    ``gc_stores.remote:open``) открывается обычным ``aopen_configured``."""
    from gc_core.config import load_settings
    from gc_stores.configured import aopen_configured
    from gc_stores.remote import RemoteThreadStore

    proxy = await ThreadProxy.astart(MemoryThreadStore(), session_checks.KEY)
    monkeypatch.setenv("GC_THREAD_SOCKET", proxy.socket_path)
    monkeypatch.setenv("GC_THREAD_TOKEN", proxy.token)
    config = tmp_path / "config.yml"
    config.write_text(
        "storage:\n  threads:\n    type: gc_stores.remote:open\n"
        "    socket: ${ENV:GC_THREAD_SOCKET}\n"
        "    token: ${ENV:GC_THREAD_TOKEN}\n"
    )
    stores = await aopen_configured(load_settings(str(config)))
    try:
        assert isinstance(stores.threads, RemoteThreadStore)
        await session_checks.check_session_reopens(stores.threads)
    finally:
        await stores.aclose()
        await proxy.aclose()
