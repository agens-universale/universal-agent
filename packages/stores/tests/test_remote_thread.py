"""Прокси одного треда: песочница swarm ходит в хранилище тредов сервера
только своим тредом и только операциями хода — без учётных данных базы.

Сервер поднимает на задачу unix-сокет с токеном; клиент
(``gc_stores.remote``) — обычный ``ThreadStore``, отказы контракта приходят
своим типом."""

import asyncio
from datetime import timedelta

import pytest
from gc_core.threads.errors import OwnershipError, ThreadConflict
from gc_core.threads.models import (
    LeaseRequest,
    SideChange,
    ThreadChange,
    ThreadKey,
    ThreadQuery,
)
from gc_stores.remote import RemoteThreadStore
from gc_stores.remote import open as open_remote
from gc_stores.remote.server import ThreadProxy
from gc_testkit import session_checks
from gc_testkit.memory_thread_store_v2 import MemoryThreadStore

# Прокси — unix-сокет (swarm только для Linux); на Windows их в asyncio нет.
pytestmark = pytest.mark.skipif(
    not hasattr(asyncio, "start_unix_server"),
    reason="прокси треда — unix-сокет, на этой платформе его нет",
)

KEY = ThreadKey(system="generic", thread="task1")


async def _pair(store=None, key=KEY):
    proxy = await ThreadProxy.astart(store or MemoryThreadStore(), key)
    client = open_remote({"socket": proxy.socket_path, "token": proxy.token})
    return proxy, client


class _ServerWritesAside(RemoteThreadStore):
    """Песочница через прокси; секции сервера (их в проверке пишет «чужой
    процесс») — мимо прокси, как их и пишет сервер."""

    def __init__(self, client: RemoteThreadStore, backing) -> None:
        super().__init__(client.socket, client._token)
        self._backing = backing

    async def append_side(self, key, change):
        sections = [change.set_frontend, change.patch_frontend]
        if any(s is not None and s[0] != "tui" for s in sections):
            return await self._backing.append_side(key, change)
        return await super().append_side(key, change)


async def test_session_of_the_task_works_through_the_proxy():
    """Путь хода (сессия агента) через прокси: шаг, чужие секции, сжатие,
    повторное открытие."""
    for check in session_checks.CHECKS:
        backing = MemoryThreadStore()
        proxy, client = await _pair(backing, key=session_checks.KEY)
        try:
            await check(_ServerWritesAside(client, backing))
        finally:
            await proxy.aclose()


async def test_contract_errors_arrive_by_type():
    proxy, client = await _pair()
    try:
        await client.claim(
            KEY, LeaseRequest(owner="sandbox", ttl=timedelta(minutes=1))
        )
        with pytest.raises(OwnershipError):
            await client.claim(
                KEY, LeaseRequest(owner="other", ttl=timedelta(minutes=1))
            )
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
#: удаление тредов, алиасы, чистка квитанций, чужой тред, входящие сервера.
_REFUSED = {
    "check_side_creates_thread",
    "check_inbox_moves_updated_at",
    "check_missing_thread",
    "check_untimed_lease",
    "check_frontend_streams",
    "check_concurrent_create",
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
    "check_conditional_frontend",
    "check_lease_token_fences_commit_and_release",
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


async def test_sandbox_cannot_strip_server_marks_through_the_proxy(tmp_path):
    """Песочница — недоверенный писатель: метку задачи swarm (по ней
    восстановление вебхука не берёт тред) она не снимет ни ``set_extra``
    поверх файлового хранилища, ни своей секцией фронтенда; секции
    сервера и чужие потоки ей не писать, бессрочно тред не занять."""
    from datetime import timedelta

    from gc_core.threads.facade import Threads
    from gc_core.threads.file_store import FileThreadStore

    store = FileThreadStore(str(tmp_path))
    await Threads(store).update_frontend(KEY, "swarm", {"swarm_id": "s1"})
    proxy, client = await _pair(store)
    try:
        record = await client.claim(
            KEY, LeaseRequest(owner="sandbox", ttl=timedelta(minutes=1))
        )
        await client.commit(
            KEY,
            ThreadChange(
                op_id="wipe",
                expected_revision=record.revision,
                lease=record.lease,
                set_extra={"frontends": {}, "tui": {"title": "x"}},
            ),
        )
        for change in (
            SideChange(op_id="w", patch_frontend=("webhook", {"status": "x"})),
            SideChange(op_id="s", set_frontend=("swarm", {})),
            SideChange(op_id="t", append=[("a2a.tasks", [{"id": "t"}])]),
        ):
            with pytest.raises(PermissionError):
                await client.append_side(KEY, change)
        with pytest.raises(PermissionError):
            await client.claim(KEY, LeaseRequest(owner="forever", ttl=None))
        after = await store.get_thread(KEY)
        assert after.frontend.get("swarm") == {"swarm_id": "s1"}
        assert "webhook" not in after.frontend
    finally:
        await proxy.aclose()


async def test_proxy_serves_a_bounded_number_of_connections(monkeypatch):
    """Чужие соединения не съедают память сервера: прокси держит не больше
    ``MAX_CONNECTIONS`` разом и не ждёт молчащий запрос вечно."""
    from gc_stores.remote import server as proxy_server

    monkeypatch.setattr(proxy_server, "READ_TIMEOUT_SECONDS", 0.3)
    proxy, client = await _pair()
    try:
        idle = [
            await asyncio.open_unix_connection(proxy.socket_path)
            for _ in range(proxy_server.MAX_CONNECTIONS + 2)
        ]
        # Молчащие соединения заняли слоты, но запрос задачи пройдёт, когда
        # их отпустит таймаут чтения.
        record = await asyncio.wait_for(
            client.get_thread(KEY), proxy_server.READ_TIMEOUT_SECONDS + 5
        )
        assert record is None
        for _reader, writer in idle:
            writer.close()
    finally:
        await proxy.aclose()


async def test_sandbox_cannot_take_a_lease_for_years():
    """Аренда на годы — та же бессрочная: тред задачи заняла бы песочница
    навсегда."""
    from gc_stores.remote import server as proxy_server

    proxy, client = await _pair()
    try:
        with pytest.raises(PermissionError):
            await client.claim(
                KEY,
                LeaseRequest(
                    owner="sandbox",
                    ttl=proxy_server.MAX_LEASE_TTL + timedelta(seconds=1),
                ),
            )
        lease = await client.claim(
            KEY, LeaseRequest(owner="sandbox", ttl=proxy_server.MAX_LEASE_TTL)
        )
        assert lease.lease is not None
    finally:
        await proxy.aclose()


async def test_connections_over_the_cap_are_closed_at_once():
    """Соединения сверх ``MAX_OPEN`` не ждут слота с открытым сокетом —
    сервер закрывает их сразу."""
    from gc_stores.remote import server as proxy_server

    proxy, _client = await _pair()
    extra = 5
    opened = []
    try:
        for _ in range(proxy_server.MAX_OPEN + extra):
            opened.append(
                await asyncio.open_unix_connection(proxy.socket_path)
            )
        await asyncio.sleep(0.2)
        closed = 0
        for reader, _writer in opened:
            try:
                if await asyncio.wait_for(reader.read(), 0.05) == b"":
                    closed += 1
            except TimeoutError:
                pass
        assert closed >= extra
    finally:
        for _reader, writer in opened:
            writer.close()
        await proxy.aclose()


async def test_client_that_never_reads_the_reply_does_not_hold_a_slot(
    monkeypatch,
):
    """Ответ, который клиент не читает, не держит слот вечно: отдача — с
    таймаутом."""
    from gc_stores.remote import protocol
    from gc_stores.remote import server as proxy_server

    monkeypatch.setattr(proxy_server, "READ_TIMEOUT_SECONDS", 0.3)
    backing = MemoryThreadStore()
    proxy, client = await _pair(backing)
    stuck = []
    try:
        await client.claim(
            KEY, LeaseRequest(owner="sandbox", ttl=timedelta(minutes=1))
        )
        ref = await backing.put_blob(
            KEY, b"\0" * (4 * 1024 * 1024), mime="application/octet-stream"
        )
        request = protocol.encode(
            {
                "token": proxy.token,
                "op": "get_blob",
                "key": protocol.wire(KEY),
                "args": {"ref": protocol.wire(ref)},
            }
        )
        for _ in range(proxy_server.MAX_CONNECTIONS):
            reader, writer = await asyncio.open_unix_connection(
                proxy.socket_path
            )
            writer.write(request)
            await writer.drain()
            stuck.append(writer)
        record = await asyncio.wait_for(
            client.get_thread(KEY), proxy_server.READ_TIMEOUT_SECONDS + 5
        )
        assert record is not None
    finally:
        for writer in stuck:
            writer.close()
        await proxy.aclose()


async def test_sandbox_cannot_read_host_files_through_get_blob(tmp_path):
    """Вложение — только файл треда задачи: имя с путём (абсолютным или с
    ``..``) песочница не подставит, чтобы прочесть файл хоста."""
    from gc_core.threads.file_store import FileThreadStore
    from gc_core.threads.models import BlobRef

    secret = tmp_path / "config_server.yml"
    secret.write_text("dsn: postgres://admin:HOSTPW@db/prod")
    proxy, client = await _pair(FileThreadStore(str(tmp_path / "threads")))
    try:
        for name in (str(secret), "../../config_server.yml", "x/../../y"):
            ref = BlobRef(key=KEY, name=name, mime="text/plain", location="")
            with pytest.raises((PermissionError, FileNotFoundError)):
                data = await client.get_blob(ref)
                assert b"HOSTPW" not in data
    finally:
        await proxy.aclose()


async def test_proxy_frame_is_bounded():
    """Ревью, раунд 2 (R17): кадр прокси — не больше 16 МиБ (память
    сервера на соединение): вложение на 4 МиБ проходит, на 13 МиБ (base64 —
    больше кадра) — отказ, а хранилище сервера его не получает."""
    backing = MemoryThreadStore()
    proxy, client = await _pair(backing)
    try:
        await client.claim(
            KEY, LeaseRequest(owner="sandbox", ttl=timedelta(minutes=1))
        )
        small = await client.put_blob(
            KEY, b"\0" * (4 * 1024 * 1024), mime="image/png"
        )
        assert len(await backing.get_blob(small)) == 4 * 1024 * 1024
        with pytest.raises(Exception):  # кадр больше предела — отказ
            await client.put_blob(
                KEY, b"\1" * (13 * 1024 * 1024), mime="image/png"
            )
        assert len(backing._blobs) == 1
    finally:
        await proxy.aclose()


@pytest.mark.parametrize("stream", ["inbox", "control"])
async def test_sandbox_cannot_write_the_servers_streams(stream):
    """Ревью, раунд 4: входящие и отмены пишет сервер. Песочница, дописав
    себе сообщение во входящие, продолжала бы себя новыми ходами и
    процессами мимо потолка задачи; запись в ``control`` подделала бы
    отмену."""
    proxy, client = await _pair()
    try:
        with pytest.raises(PermissionError):
            await client.append_side(
                KEY,
                SideChange(
                    op_id=f"forged-{stream}",
                    append=[(stream, [{"role": "user", "content": "ещё"}])],
                ),
            )
    finally:
        await proxy.aclose()


async def test_sandbox_writes_the_streams_of_its_turn():
    """Потоки хода — трасса субагентов, журнал правок, лента TUI —
    песочнице открыты."""
    proxy, client = await _pair()
    try:
        for stream in ("subagent_events", "edits", "tui.events"):
            await client.append_side(
                KEY,
                SideChange(op_id=f"own-{stream}", append=[(stream, [{}])]),
            )
    finally:
        await proxy.aclose()
