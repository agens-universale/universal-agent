"""Восстановление прерванных задач вебхука после падения (спека сервера, 13.4).

Тред, у которого ход был отмечен идущим (``runtime.turn == "running"``), а
аренды больше нет, или во входящем потоке которого лежит недоставленное, —
бесхозный. Обход инстанса берёт такой тред (захват аренды выигрывает один) и
продолжает ходом с техническим сообщением о сбое плюс недоставленные.
"""

import asyncio
import json

import httpx
from gc_core.execution.environment import utc_now
from gc_core.threads.facade import Threads
from gc_core.threads.models import ThreadKey
from gc_core.threads.session_port import SessionPort
from gc_server.app import ServerRuntime
from gc_server.config import ServerConfig
from gc_server.stores import aopen_process_stores
from gc_server.webhook import status as task_status
from gc_server.webhook.recovery import RECOVERY_MESSAGE, arecover_once
from gc_server.webhook.surface import WebhookSurface
from gc_testkit import scripted_model
from gc_testkit.settings import make_settings

KEY = ThreadKey(system="generic", thread="t")


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


def _install(monkeypatch):
    from gc_core.utils import connect_backend

    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content or b"{}"))
        return httpx.Response(200, json=scripted_model.FINISH)

    monkeypatch.setattr(
        connect_backend,
        "get_async_client",
        lambda **_kw: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    return seen


async def _surface(tmp_path, stores, recovery=None):
    config = {"webhook": {"recovery": recovery}} if recovery else {}
    return WebhookSurface(
        ServerRuntime(
            base=_settings(tmp_path),
            config=ServerConfig.model_validate(config),
            stores=stores,
        )
    )


async def _crashed_run(store, key=KEY):
    """Ход умер посреди работы: история записана, отметка хода стоит,
    аренды нет (истекла), статус задачи — running прежнего инстанса."""
    dead = SessionPort(store, owner="dead/1/deadbeef")
    await dead.aappend_messages(
        key.system,
        key.thread,
        [{"id": "u1", "role": "user", "content": "задача"}],
        known_keys=set(),
    )
    await dead.amark_turn(key.system, key.thread, running=True)
    await dead.arelease_lease(key.system, key.thread)
    await task_status.aset_status(
        Threads(store), key, status="running", run_id="dead:1", started_at=1.0
    )


async def _settled(*surfaces) -> None:
    for _ in range(300):
        tasks = [t for s in surfaces for t in s.registry.live_tasks()]
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)
    raise AssertionError("прогоны восстановления не закончились")


def _mentions(payload, text: str) -> bool:
    return any(
        text in str(m.get("content"))
        for m in payload.get("messages", [])
        if m.get("role") == "user"
    )


async def test_interrupted_task_is_continued(monkeypatch, tmp_path):
    seen = _install(monkeypatch)
    stores = await aopen_process_stores(_settings(tmp_path))
    surface = await _surface(tmp_path, stores)
    statuses: list = []
    real = task_status.aset_status

    async def spy(threads, key, **fields):
        statuses.append(fields.get("status"))
        await real(threads, key, **fields)

    monkeypatch.setattr(task_status, "aset_status", spy)
    try:
        await _crashed_run(stores.threads)
        statuses.clear()
        taken = await arecover_once(surface, now=utc_now())
        await _settled(surface)
        record = await stores.threads.get_thread(KEY)
    finally:
        await stores.aclose()
    assert taken == [KEY]
    assert len(seen) == 1 and _mentions(seen[0], RECOVERY_MESSAGE)
    assert [s for s in statuses if s] == [
        "interrupted",
        "running",
        "completed",
    ]
    assert record.runtime.turn is None


async def test_live_foreign_lease_is_left_alone(monkeypatch, tmp_path):
    seen = _install(monkeypatch)
    stores = await aopen_process_stores(_settings(tmp_path))
    surface = await _surface(tmp_path, stores)
    try:
        await _crashed_run(stores.threads)
        await SessionPort(stores.threads, owner="alive").aclaim(
            KEY.system, KEY.thread
        )
        taken = await arecover_once(surface, now=utc_now())
    finally:
        await stores.aclose()
    assert taken == [] and seen == []


async def test_undelivered_inbox_without_owner_is_played(
    monkeypatch, tmp_path
):
    seen = _install(monkeypatch)
    stores = await aopen_process_stores(_settings(tmp_path))
    surface = await _surface(tmp_path, stores)
    try:
        await Threads(stores.threads).submit(
            KEY, {"role": "user", "content": "никто не забрал"}
        )
        taken = await arecover_once(surface, now=utc_now())
        await _settled(surface)
    finally:
        await stores.aclose()
    assert taken == [KEY]
    assert _mentions(seen[0], "никто не забрал")
    assert not _mentions(seen[0], RECOVERY_MESSAGE)


async def test_two_instances_continue_the_task_once(monkeypatch, tmp_path):
    seen = _install(monkeypatch)
    stores = await aopen_process_stores(_settings(tmp_path))
    first = await _surface(tmp_path, stores)
    second = await _surface(tmp_path, stores)
    try:
        await _crashed_run(stores.threads)
        await asyncio.gather(
            arecover_once(first, now=utc_now()),
            arecover_once(second, now=utc_now()),
        )
        await _settled(first, second)
        history = await Threads(stores.threads).history(KEY)
    finally:
        await stores.aclose()
    assert len(seen) == 1
    assert (
        sum(RECOVERY_MESSAGE in str(e.data.get("content")) for e in history)
        == 1
    )


async def test_threads_of_other_surfaces_are_not_touched(
    monkeypatch, tmp_path
):
    seen = _install(monkeypatch)
    stores = await aopen_process_stores(_settings(tmp_path))
    surface = await _surface(tmp_path, stores)
    try:
        for system in ("a2a", "responses", "cli", "tui"):
            await _crashed_run(
                stores.threads, ThreadKey(system=system, thread="t")
            )
        taken = await arecover_once(surface, now=utc_now())
    finally:
        await stores.aclose()
    assert taken == [] and seen == []


async def test_recovery_switch(tmp_path):
    stores = await aopen_process_stores(_settings(tmp_path))
    off = await _surface(tmp_path, stores, recovery={"enabled": False})
    on = await _surface(
        tmp_path, stores, recovery={"enabled": True, "interval_seconds": 60}
    )
    try:
        await off.astart()
        await on.astart()
        assert off.recovery_task is None
        assert on.recovery_task is not None
    finally:
        await off.astop()
        await on.astop()
        await stores.aclose()
    assert on.recovery_task is None


async def test_interruption_is_reported_by_callback(monkeypatch, tmp_path):
    _install(monkeypatch)
    stores = await aopen_process_stores(_settings(tmp_path))
    surface = await _surface(tmp_path, stores)
    got: list = []
    surface.callbacks.config = surface.config.callbacks.model_copy(
        update={"url": "https://hooks.example/done"}
    )
    surface.config = surface.config.model_copy(
        update={"callbacks": surface.callbacks.config}
    )
    surface.callbacks.client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: got.append(json.loads(r.content)) or httpx.Response(200)
        )
    )
    try:
        await _crashed_run(stores.threads)
        await arecover_once(surface, now=utc_now())
        await _settled(surface)
        await surface.callbacks.adrain()
    finally:
        await stores.aclose()
    assert [e["type"] for e in got] == ["task.interrupted", "task.completed"]
