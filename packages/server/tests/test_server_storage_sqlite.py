"""gc-serve на хранилище из storage: config.yml — без внешних сервисов (SQLite).

Вебхук пишет тред в базу, /logs читает его оттуда, в каталоге тредов пусто;
хранилище открывается до портов и закрывается после остановки.
"""

import asyncio

import httpx
import pytest
from gc_core.threads.models import ThreadKey
from gc_server.app import ServerRuntime, build_surface
from gc_server.config import ServerConfig
from gc_server.stores import aopen_process_stores
from gc_stores.sqlite import SQLiteThreadStore
from gc_testkit import scripted_model
from gc_testkit.settings import make_settings
from server_harness import LOOPBACK_URL


def _settings(tmp_path):
    return make_settings(
        {
            "backend.api_mode": "chat_completions",
            "backend.completions_url": "http://scripted.invalid/v1/chat/completions",
            "backend.chat_completion_kwargs": {"stream": False},
            "agent.model": "scripted",
            "agent.max_steps": 3,
            "agent.use_local_subagents": False,
            "agent.strict_turn_finish_validation": False,
            "logging.save_logs": False,
            "logging.console_trace": False,
            "paths.threads_data_path": str(tmp_path / "threads"),
            "storage": {
                "threads": {"type": "sqlite", "path": str(tmp_path / "t.db")}
            },
        }
    )


async def _wait_done(client, path: str) -> dict:
    for _ in range(200):
        body = (await client.get(path)).json()
        if body.get("status") not in ("running", None):
            return body
        await asyncio.sleep(0.05)
    raise AssertionError(f"прогон не закончился: {body}")


async def test_webhook_thread_lives_in_the_configured_database(
    tmp_path, monkeypatch
):
    scripted_model.install(monkeypatch)
    loaded = _settings(tmp_path)
    stores = await aopen_process_stores(loaded)
    assert stores is not None
    runtime = ServerRuntime(base=loaded, config=ServerConfig(), stores=stores)
    surface = build_surface(runtime, "webhook")
    transport = httpx.ASGITransport(app=surface.app())
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url=LOOPBACK_URL
        ) as client:
            started = await client.post(
                "/v1/tasks", json={"task_id": "t-db", "text": "сделай"}
            )
            assert started.json()["status"] == "started"
            done = await _wait_done(client, "/v1/tasks/generic/t-db")
            assert done["status"] == "completed", done
            logs = (await client.get("/v1/tasks/generic/t-db/messages")).json()
    finally:
        await surface.astop()
        await stores.aclose()
    assert logs["messages"]
    async with SQLiteThreadStore(str(tmp_path / "t.db")) as store:
        record = await store.get_thread(
            ThreadKey(system="generic", thread="t-db")
        )
    assert record is not None and record.info.messages_count > 0
    threads_dir = tmp_path / "threads"
    assert not threads_dir.exists() or not any(
        p.name.startswith("data_") for p in threads_dir.rglob("*")
    )


async def test_files_storage_opens_nothing(tmp_path):
    loaded = make_settings({"paths.threads_data_path": str(tmp_path)})
    assert await aopen_process_stores(loaded) is None


@pytest.mark.parametrize("part", ["threads", "agent"])
def test_runtime_passes_only_non_file_parts(tmp_path, part):
    from gc_core.stores import Stores

    sentinel = object()
    kinds = {"threads": "files", "agent": "files", part: "sqlite"}
    loaded = make_settings(
        {
            "storage": {
                name: {"type": kind, "path": "x", "agent_id": "a"}
                if kind != "files"
                else {}
                for name, kind in kinds.items()
            }
        }
    )
    stores = Stores(
        threads=sentinel if part == "threads" else object(),
        agent=sentinel if part == "agent" else object(),
    )
    runtime = ServerRuntime(base=loaded, config=ServerConfig(), stores=stores)
    key = "thread_store" if part == "threads" else "agent_store"
    assert runtime.store_kwargs() == {key: sentinel}


async def test_cleanup_on_a_database_prunes_only_webhook_threads(tmp_path):
    """Чистка тредов на хранилище в базе идёт через фасад и трогает только
    треды вебхука: диалоги chat и сессии CLI в той же базе остаются."""
    from datetime import timedelta

    from gc_core.threads.facade import Threads
    from gc_server.webhook.surface import WebhookSurface

    from gc_server.webhook import status as task_status

    loaded = _settings(tmp_path)
    stores = await aopen_process_stores(loaded)
    threads = Threads(stores.threads)
    for system, thread in (
        ("generic", "old"),
        ("generic", "own"),
        ("responses", "old"),
        ("cli", "old"),
    ):
        await threads.append_messages(
            ThreadKey(system=system, thread=thread),
            [{"role": "user", "content": "x", "id": f"m-{system}-{thread}"}],
        )
    # Задача вебхука — со статусом вебхука; ``generic/own`` — тред той же
    # системы, но не задача (например, gc-cli --system generic).
    await task_status.aset_status(
        threads, ThreadKey(system="generic", thread="old"), status="completed"
    )
    runtime = ServerRuntime(
        base=loaded,
        config=ServerConfig.model_validate(
            {"webhook": {"threads_cleanup": {"enabled": True, "ttl_days": 1}}}
        ),
        stores=stores,
    )
    surface = WebhookSurface(runtime)
    try:
        removed = await surface.aprune_once(older_than=timedelta(seconds=-1))
        left = {r.key.system async for r in threads.iter_threads()}
        own = await threads.get(ThreadKey(system="generic", thread="own"))
    finally:
        await stores.aclose()
    assert removed == 1
    assert left == {"generic", "responses", "cli"}
    assert own is not None


async def test_cleanup_on_files_prunes_only_webhook_threads(tmp_path):
    """Каталог тредов сервера по умолчанию общий с gc-cli и TUI: уборщик на
    файлах идёт тем же фасадом, что и на базе, и трогает только задачи
    вебхука — сессии пользователя в том же каталоге остаются."""
    import asyncio

    from gc_core.threads.facade import Threads
    from gc_core.threads.file_store import FileThreadStore
    from gc_server.webhook import status as task_status
    from gc_server.webhook.surface import WebhookSurface

    threads_dir = tmp_path / "threads"
    threads = Threads(FileThreadStore(threads_dir))
    task = ThreadKey(system="generic", thread="old")
    session = ThreadKey(system="cli", thread="old")
    for key in (task, session):
        await threads.append_messages(
            key, [{"role": "user", "content": "x", "id": f"m-{key.system}"}]
        )
    await task_status.aset_status(threads, task, status="completed")
    await asyncio.sleep(1.1)  # старше срока (ttl ≈ 0,9 с)
    runtime = ServerRuntime(
        base=make_settings({"paths.threads_data_path": str(threads_dir)}),
        config=ServerConfig.model_validate(
            {
                "webhook": {
                    "recovery": {"enabled": False},
                    "threads_cleanup": {
                        "enabled": True,
                        "ttl_days": 0.00001,
                        "interval_seconds": 3600,
                    },
                }
            }
        ),
    )
    surface = WebhookSurface(runtime)
    await surface.astart()
    try:
        for _ in range(200):
            if await threads.get(task) is None:
                break
            await asyncio.sleep(0.02)
    finally:
        await surface.astop()
    assert await threads.get(task) is None
    assert await threads.get(session) is not None


async def test_maintenance_sweeps_orphans_of_a_kv_store(tmp_path):
    """Обслуживание вебхука (раз в час) — квитанции и уборка сирот
    прерванных записей у хранилища, которое её умеет (Redis, S3)."""
    from datetime import timedelta

    from gc_core.stores import Stores
    from gc_server.webhook.surface import WebhookSurface
    from gc_testkit.memory_thread_store_v2 import MemoryThreadStore

    swept: list = []

    class Sweeping(MemoryThreadStore):
        async def sweep(self, older_than=timedelta(hours=1)):
            swept.append(older_than)
            return 0

    loaded = make_settings(
        {
            "paths.threads_data_path": str(tmp_path),
            "storage": {"threads": {"type": "redis", "url": "redis://x"}},
        }
    )
    runtime = ServerRuntime(
        base=loaded,
        config=ServerConfig(),
        stores=Stores(threads=Sweeping(), agent=object()),
    )
    await WebhookSurface(runtime).amaintain_once()
    assert len(swept) == 1


async def test_cleanup_skips_a_task_under_a_live_lease(tmp_path):
    """Ревью, раунд 2 (R15): продление аренды не двигает ``updated_at`` —
    задача с долгим шагом выглядит старой, но её ход жив: уборщик её не
    удаляет посреди хода."""
    from datetime import timedelta

    from gc_core.threads.facade import Threads
    from gc_core.threads.models import LeaseRequest
    from gc_server.webhook import status as task_status
    from gc_server.webhook.surface import WebhookSurface

    loaded = _settings(tmp_path)
    stores = await aopen_process_stores(loaded)
    threads = Threads(stores.threads)
    key = ThreadKey(system="generic", thread="busy")
    await task_status.aset_status(threads, key, status="running")
    await stores.threads.claim(
        key, LeaseRequest(owner="turn", ttl=timedelta(minutes=5))
    )
    surface = WebhookSurface(
        ServerRuntime(base=loaded, config=ServerConfig(), stores=stores)
    )
    try:
        removed = await surface.aprune_once(older_than=timedelta(seconds=-1))
        alive = await threads.get(key)
    finally:
        await stores.aclose()
    assert removed == 0
    assert alive is not None
