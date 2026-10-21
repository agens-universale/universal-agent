"""Тред задачи swarm в хранилище тредов сервера (``swarm.thread_in_store``).

``gc-cli`` в песочнице ведёт тред задачи (``-t``, ``--system``) через прокси
одного треда: unix-сокет и токен на задачу, учётных данных хранилища
песочница не получает. ``/logs`` swarm-порта читает тред из хранилища,
сообщение в идущую задачу ложится во входящий поток (``queued``) — его
забирает опрос ядра в песочнице. Без флага — тред в файлах песочницы (409).
"""

import asyncio
import os
import shutil

import pytest
from fastapi.testclient import TestClient
from gc_core.stores import Stores
from gc_core.threads.facade import Threads, run_sync
from gc_core.threads.models import ThreadKey
from gc_server.app import ServerRuntime
from gc_server.config import ServerConfig
from gc_server.swarm import storage
from gc_server.swarm.surface import SwarmSurface
from gc_stores.remote import open as open_remote
from gc_testkit.memory_thread_store_v2 import MemoryThreadStore
from gc_testkit.settings import make_settings
from server_harness import LOOPBACK_URL

KEY = ThreadKey(system="g", thread="t1")
#: Хранилище сервера в базе (само не открывается: треды — в памяти теста).
SERVER_DB = {"threads": {"type": "postgres", "dsn": "postgresql://u:pw@db/a"}}


def _swarm_cfg(tmp_path, **extra):
    threads_dir, sandbox = tmp_path / "threads", tmp_path / "sandbox"
    threads_dir.mkdir(exist_ok=True)
    sandbox.mkdir(exist_ok=True)
    return {
        "threads_dir": str(threads_dir),
        "sandbox_root_dir": str(sandbox),
        "max_concurrent": 10,
        "ttl_seconds": 86400,
        "limits": {},
        "cli_command": "gc-cli",
        "wrapper_path": "/usr/bin/true",
        "sandbox_tool": "bubblewrap",
        "agentmd_path": "",
        "skills_dir": "",
        **extra,
    }


def _runtime(tmp_path, storage_section=None, store=None):
    loaded = make_settings(
        {
            "paths.threads_data_path": str(tmp_path / "files"),
            **({"storage": storage_section} if storage_section else {}),
        }
    )
    return ServerRuntime(
        base=loaded,
        config=ServerConfig(),
        stores=Stores(threads=store or MemoryThreadStore(), agent=object()),
    )


@pytest.fixture
def surface(tmp_path, monkeypatch):
    store = MemoryThreadStore()
    surface = SwarmSurface(
        _runtime(tmp_path, SERVER_DB, store=store),
        swarm=_swarm_cfg(tmp_path, thread_in_store=True),
    )
    started: list = []

    def fake_start(
        swarm_cfg,
        *,
        system,
        task_id,
        payload,
        trace_id=None,
        query=None,
        thread=None,
    ):
        sid = f"swarm_{system}_{task_id}"
        started.append(thread)
        storage.save_swarm_meta(
            swarm_cfg["threads_dir"],
            sid,
            base_meta={"system": system, "task_id": task_id},
            swarm_meta_patch={"id": sid, "status": "running", "pid": 99999},
        )
        return sid, 99999

    monkeypatch.setattr(
        "gc_server.swarm.runner.start_swarm_process", fake_start
    )
    monkeypatch.setattr("os.kill", lambda pid, sig: None)
    surface.started = started
    surface.store = store
    yield surface
    # Цикл TestClient уже закрыт: каталоги сокетов убираем сами.
    for access in started:
        if access is not None:
            shutil.rmtree(access.socket_dir, ignore_errors=True)


def _client_of(access):
    return open_remote(
        {
            "socket": os.path.join(access.socket_dir, "thread.sock"),
            "token": access.token,
        }
    )


def test_flag_comes_from_the_swarm_section(tmp_path):
    runtime = _runtime(tmp_path)
    runtime = ServerRuntime(
        base=runtime.base,
        config=ServerConfig.model_validate(
            {"swarm": {"thread_in_store": True}}
        ),
        stores=runtime.stores,
    )
    built = SwarmSurface(runtime)
    assert built.swarm["thread_in_store"] is True


def test_without_the_flag_the_sandbox_stays_on_files(tmp_path):
    surface = SwarmSurface(_runtime(tmp_path), swarm=_swarm_cfg(tmp_path))
    assert not surface._in_store


def test_each_task_reaches_only_its_thread(surface):
    """Прокси задачи отдаёт только её тред; токен другой задачи к нему
    не подходит."""
    with TestClient(surface.app(), base_url=LOOPBACK_URL) as client:
        client.post("/v1/tasks", json={"system": "g", "task_id": "t1"})
        client.post("/v1/tasks", json={"system": "g", "task_id": "t2"})
        first, second = surface.started
        assert first.token != second.token
        own = _client_of(first)
        record = asyncio.run(own.get_thread(KEY))
        assert "swarm" in record.frontend
        with pytest.raises(PermissionError):
            asyncio.run(own.get_thread(ThreadKey(system="g", thread="t2")))
        stolen = _client_of(type(first)(first.socket_dir, second.token))
        with pytest.raises(PermissionError):
            asyncio.run(stolen.get_thread(KEY))


def test_proxy_closes_when_the_task_ends(surface):
    with TestClient(surface.app(), base_url=LOOPBACK_URL) as client:
        client.post("/v1/tasks", json={"system": "g", "task_id": "t1"})
        (access,) = surface.started
        assert os.path.exists(os.path.join(access.socket_dir, "thread.sock"))
        storage.save_swarm_meta(
            surface.threads_dir,
            "swarm_g_t1",
            base_meta={"system": "g", "task_id": "t1"},
            swarm_meta_patch={"status": "completed"},
        )
        client.portal.call(surface._afollow_finished_tasks)
        assert not os.path.exists(access.socket_dir)


def test_server_stop_closes_the_proxies(surface):
    with TestClient(surface.app(), base_url=LOOPBACK_URL) as client:
        client.post("/v1/tasks", json={"system": "g", "task_id": "t1"})
        (access,) = surface.started
        client.portal.call(surface.astop)
        assert not os.path.exists(access.socket_dir)


def test_failed_start_closes_the_proxy(surface, monkeypatch):
    seen = []

    def broken_start(swarm_cfg, *, thread=None, **_kw):
        seen.append(thread)
        raise OSError("нет обёртки")

    monkeypatch.setattr(
        "gc_server.swarm.runner.start_swarm_process", broken_start
    )
    with TestClient(surface.app(), base_url=LOOPBACK_URL) as client:
        resp = client.post("/v1/tasks", json={"system": "g", "task_id": "t1"})
    assert resp.status_code == 500
    assert not os.path.exists(seen[0].socket_dir)


def test_storage_secrets_stay_out_of_the_sandbox_environment(
    tmp_path, monkeypatch
):
    """Подключение хранилища сервера из окружения (``${ENV:…}``) и
    переменные драйвера в песочницу не уходят: агент там читает своё
    окружение."""
    from gc_core.config import load_settings

    monkeypatch.setenv("SRV_PG_DSN", "postgresql://srv:s3cret@db/gc")
    config = tmp_path / "config.yml"
    config.write_text(
        "storage:\n  threads:\n    type: postgres\n"
        "    dsn: ${ENV:SRV_PG_DSN}\n"
    )
    surface = SwarmSurface(
        ServerRuntime(
            base=load_settings(str(config)),
            config=ServerConfig(),
            stores=Stores(threads=MemoryThreadStore(), agent=object()),
        ),
        swarm=_swarm_cfg(tmp_path),
    )
    assert {"SRV_PG_DSN", "PGPASSWORD"} <= set(surface.swarm["hidden_env"])


def test_thread_in_store_works_on_files(tmp_path, surface, monkeypatch):
    """Хранилище сервера на файлах: тред задачи — в каталоге тредов
    поверхности, песочница ходит в него тем же прокси."""
    files = SwarmSurface(
        _runtime(tmp_path),
        swarm=_swarm_cfg(tmp_path, thread_in_store=True),
    )
    with TestClient(files.app(), base_url=LOOPBACK_URL) as client:
        client.post("/v1/tasks", json={"system": "g", "task_id": "t1"})
        (access,) = surface.started
        record = asyncio.run(_client_of(access).get_thread(KEY))
        assert "swarm" in record.frontend
        client.portal.call(files.astop)


def test_logs_do_not_read_threads_of_other_tasks(surface):
    """Тред вебхука с тем же (system, task_id) — не задача swarm: /logs
    swarm-порта его не отдаёт."""
    run_sync(
        Threads(surface.store).append_messages(
            ThreadKey(system="g", thread="hook"),
            [{"id": "s1", "role": "user", "content": "секрет вебхука"}],
        )
    )
    resp = TestClient(surface.app(), base_url=LOOPBACK_URL).get(
        "/v1/tasks/g/hook/messages"
    )
    assert resp.status_code == 404


def test_message_into_a_running_task_is_queued_into_the_inbox(surface):
    client = TestClient(surface.app(), base_url=LOOPBACK_URL)
    first = client.post("/v1/tasks", json={"system": "g", "task_id": "t1"})
    assert first.json()["status"] == "started"
    again = client.post(
        "/v1/tasks",
        json={"system": "g", "task_id": "t1", "text": "и тесты"},
    )
    assert again.status_code == 202
    assert again.json()["status"] == "queued"
    entries = run_sync(surface.store.read_stream(KEY, "inbox"))
    assert "и тесты" in entries[-1].data["content"]
    assert len(surface.started) == 1


def test_task_thread_is_marked_as_swarm(surface):
    """Восстановление вебхука такой тред не берёт: его ведёт песочница."""
    TestClient(surface.app(), base_url=LOOPBACK_URL).post(
        "/v1/tasks", json={"system": "g", "task_id": "t1"}
    )
    record = run_sync(surface.store.get_thread(KEY))
    assert "swarm" in record.frontend


def test_logs_come_from_the_store(surface):
    client = TestClient(surface.app(), base_url=LOOPBACK_URL)
    client.post("/v1/tasks", json={"system": "g", "task_id": "t1"})
    run_sync(
        Threads(surface.store).append_messages(
            KEY, [{"id": "u1", "role": "user", "content": "из песочницы"}]
        )
    )
    body = client.get("/v1/tasks/g/t1/messages").json()
    assert [m["message"]["content"] for m in body["messages"]] == [
        "из песочницы"
    ]
    assert client.get("/v1/tasks/g/t1").json()["swarm"]["status"] == "running"


def test_swarm_does_not_take_over_a_webhook_thread(surface):
    """Тред вебхука с тем же (system, task_id) — чужой: swarm не помечает
    его своим и не запускает на нём песочницу (она читала бы и писала
    чужую историю)."""
    run_sync(
        Threads(surface.store).append_messages(
            KEY, [{"id": "w1", "role": "user", "content": "задача вебхука"}]
        )
    )
    resp = TestClient(surface.app(), base_url=LOOPBACK_URL).post(
        "/v1/tasks", json={"system": "g", "task_id": "t1", "message": "x"}
    )
    assert resp.status_code == 409
    assert surface.started == []
    record = run_sync(surface.store.get_thread(KEY))
    assert "swarm" not in record.frontend


def _sandbox_exited(surface, status="completed", *, sandbox_exists=True):
    sandbox = os.path.join(surface.sandbox_root_dir, "swarm_g_t1")
    if sandbox_exists:
        os.makedirs(sandbox, exist_ok=True)
    storage.save_swarm_meta(
        surface.threads_dir,
        "swarm_g_t1",
        base_meta={"system": "g", "task_id": "t1"},
        swarm_meta_patch={"status": status, "sandbox_path": sandbox},
    )


def _queued_message(client):
    client.post("/v1/tasks", json={"system": "g", "task_id": "t1"})
    resp = client.post(
        "/v1/tasks", json={"system": "g", "task_id": "t1", "text": "и тесты"}
    )
    assert resp.json()["status"] == "queued"


def _no_continuation(*_a, **_kw):
    raise AssertionError("продолжения быть не должно")


def test_undelivered_message_continues_the_task_in_its_sandbox(
    surface, monkeypatch
):
    """Сообщение принято как queued, а песочница успела выйти: сервер
    поднимает ход из входящего потока в той же песочнице, тем же прокси."""
    continued = []

    def fake_continue(swarm_cfg, *, system, task_id, thread):
        continued.append((system, task_id, thread))
        storage.save_swarm_meta(
            swarm_cfg["threads_dir"],
            "swarm_g_t1",
            base_meta={"system": system, "task_id": task_id},
            swarm_meta_patch={"status": "running", "pid": 99999},
        )
        return 99999

    monkeypatch.setattr(
        "gc_server.swarm.runner.continue_swarm_process", fake_continue
    )
    with TestClient(surface.app(), base_url=LOOPBACK_URL) as client:
        _queued_message(client)
        (access,) = surface.started
        _sandbox_exited(surface)
        client.portal.call(surface._afollow_finished_tasks)
        assert continued == [("g", "t1", access)]
        assert os.path.exists(os.path.join(access.socket_dir, "thread.sock"))
        assert client.get("/v1/tasks/g/t1").json()["status"] == "running"


def test_undelivered_message_fails_the_task_when_the_sandbox_is_gone(
    surface, monkeypatch
):
    monkeypatch.setattr(
        "gc_server.swarm.runner.continue_swarm_process", _no_continuation
    )
    with TestClient(surface.app(), base_url=LOOPBACK_URL) as client:
        _queued_message(client)
        (access,) = surface.started
        _sandbox_exited(surface, sandbox_exists=False)
        client.portal.call(surface._afollow_finished_tasks)
        body = client.get("/v1/tasks/g/t1").json()
    assert body["status"] == "failed"
    assert "сообщения не доставлены: песочница закрыта" in body["error"]
    assert body["pending_messages"] == 1
    assert not os.path.exists(access.socket_dir)


@pytest.mark.parametrize("status", ["cancelled", "failed", "timeout"])
def test_task_that_did_not_complete_is_not_continued(
    surface, monkeypatch, status
):
    """Отменённую или упавшую задачу продолжение не поднимает: недоставленное
    видно в ``pending_messages``."""
    monkeypatch.setattr(
        "gc_server.swarm.runner.continue_swarm_process", _no_continuation
    )
    with TestClient(surface.app(), base_url=LOOPBACK_URL) as client:
        _queued_message(client)
        (access,) = surface.started
        _sandbox_exited(surface, status)
        client.portal.call(surface._afollow_finished_tasks)
        body = client.get("/v1/tasks/g/t1").json()
    assert body["pending_messages"] == 1
    assert body["swarm"]["status"] == status
    assert not os.path.exists(access.socket_dir)
