"""Два инстанса сервера на одной базе (спека сервера, 13.2–13.3).

Тред ведёт тот, кто взял его аренду. Вебхук на другой инстанс кладёт
сообщение во входящий поток треда и отвечает ``queued`` — ход владельца
забирает его на следующем шаге. Статус задачи — в хранилище: ``/runs``
отвечает одинаково на любом инстансе.
"""

import asyncio
import json

import httpx
from gc_server.app import ServerRuntime
from gc_server.config import ServerConfig
from gc_server.stores import aopen_process_stores
from gc_server.webhook.surface import WebhookSurface
from gc_testkit import scripted_model
from gc_testkit.settings import make_settings

THINK = {
    **scripted_model.FINISH,
    "id": "scripted-think",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "think-1",
                        "type": "function",
                        "function": {
                            "name": "session_think",
                            "arguments": json.dumps({"thoughts": "думаю"}),
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
}


def _settings(tmp_path):
    return make_settings(
        {
            "backend.api_mode": "chat_completions",
            "backend.completions_url": "http://scripted.invalid/v1/chat/completions",
            "backend.chat_completion_kwargs": {"stream": False},
            "agent.model": "scripted",
            "agent.max_steps": 5,
            "agent.use_local_subagents": False,
            "agent.strict_turn_finish_validation": False,
            "logging.save_logs": False,
            "logging.console_trace": False,
            "paths.threads_data_path": str(tmp_path / "threads"),
            "thread_store.inbox_poll_seconds": 0.01,
            "storage": {
                "threads": {"type": "sqlite", "path": str(tmp_path / "t.db")}
            },
        }
    )


def _install(monkeypatch, plan):
    from gc_core.utils import connect_backend

    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        seen.append(payload)
        return httpx.Response(200, json=await plan(len(seen) - 1))

    monkeypatch.setattr(
        connect_backend,
        "get_async_client",
        lambda **_kw: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    return seen


def _client(surface) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=surface.app()),
        base_url="http://test",
    )


async def _wait(client, path: str, status: str) -> dict:
    for _ in range(400):
        body = (await client.get(path)).json()
        if body.get("status") == status:
            return body
        await asyncio.sleep(0.02)
    raise AssertionError(f"{path}: не дождались {status}, последнее {body}")


async def _wait_lease(surface, system: str, task_id: str) -> None:
    """Ход первого инстанса взял тред (статус ``running`` пишется чуть
    раньше захвата)."""
    from gc_core.threads.models import ThreadKey

    key = ThreadKey(system=system, thread=task_id)
    for _ in range(400):
        if await surface.threads().busy_owner(key) is not None:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("ход не взял тред")


def _user_texts(payload) -> str:
    return " ".join(
        str(m.get("content"))
        for m in payload.get("messages", [])
        if m.get("role") == "user"
    )


async def test_second_instance_queues_into_the_owners_turn(
    monkeypatch, tmp_path
):
    loaded = _settings(tmp_path)
    stores = await aopen_process_stores(loaded)
    first = WebhookSurface(
        ServerRuntime(base=loaded, config=ServerConfig(), stores=stores)
    )
    second = WebhookSurface(
        ServerRuntime(base=loaded, config=ServerConfig(), stores=stores)
    )
    go = asyncio.Event()

    async def plan(index):
        if index == 0:
            await go.wait()
            # Опрос владельца успевает положить сообщение второго в очередь.
            await asyncio.sleep(0.2)
            return THINK
        return scripted_model.FINISH

    seen = _install(monkeypatch, plan)
    try:
        async with _client(first) as a, _client(second) as b:
            started = await a.post(
                "/events/webhook", json={"task_id": "t", "text": "начни"}
            )
            assert started.json()["status"] == "started"
            await _wait(b, "/runs/generic/t", "running")
            await _wait_lease(second, "generic", "t")
            queued = await b.post(
                "/events/webhook", json={"task_id": "t", "text": "и тесты"}
            )
            assert queued.json()["status"] == "queued"
            go.set()
            done = await _wait(b, "/runs/generic/t", "completed")
            assert done["result"] is not None
            assert (await a.get("/runs/generic/t")).json()["status"] == (
                "completed"
            )
    finally:
        await first.astop()
        await second.astop()
        await stores.aclose()
    assert "и тесты" in _user_texts(seen[1])
    assert "и тесты" not in _user_texts(seen[0])


async def test_runs_of_an_unknown_task_is_404(tmp_path):
    loaded = _settings(tmp_path)
    stores = await aopen_process_stores(loaded)
    surface = WebhookSurface(
        ServerRuntime(base=loaded, config=ServerConfig(), stores=stores)
    )
    try:
        async with _client(surface) as client:
            assert (await client.get("/runs/generic/nope")).status_code == 404
    finally:
        await stores.aclose()


async def test_webhook_into_a_queued_task_after_the_turn_is_played(
    monkeypatch, tmp_path
):
    """Сообщение, пришедшее, когда ход уже всё забрал и закрывается, не
    теряется: прогон доигрывает его следующим ходом."""
    loaded = _settings(tmp_path)
    stores = await aopen_process_stores(loaded)
    surface = WebhookSurface(
        ServerRuntime(base=loaded, config=ServerConfig(), stores=stores)
    )
    holder: dict = {}

    async def plan(index):
        if index == 0:
            # Последний шаг хода: сообщение ляжет после его забора.
            holder["late"] = asyncio.ensure_future(
                holder["client"].post(
                    "/events/webhook", json={"task_id": "t", "text": "поздно"}
                )
            )
            await asyncio.sleep(0)
        return scripted_model.FINISH

    seen = _install(monkeypatch, plan)
    try:
        async with _client(surface) as client:
            holder["client"] = client
            await client.post(
                "/events/webhook", json={"task_id": "t", "text": "начни"}
            )
            await _wait(client, "/runs/generic/t", "completed")
            assert (await holder["late"]).json()["status"] in {
                "queued",
                "started",
            }
            for _ in range(200):
                if any("поздно" in _user_texts(p) for p in seen):
                    break
                await asyncio.sleep(0.02)
    finally:
        await surface.astop()
        await stores.aclose()
    assert any("поздно" in _user_texts(p) for p in seen)


async def test_cancel_from_another_instance_cancels_the_task(
    monkeypatch, tmp_path
):
    """Отмена — запись в поток control треда с любого инстанса: ход
    владельца останавливается, статус задачи — cancelled."""
    from gc_core.threads.models import ThreadKey

    loaded = _settings(tmp_path)
    stores = await aopen_process_stores(loaded)
    first = WebhookSurface(
        ServerRuntime(base=loaded, config=ServerConfig(), stores=stores)
    )
    second = WebhookSurface(
        ServerRuntime(base=loaded, config=ServerConfig(), stores=stores)
    )

    async def plan(index):
        if index == 0:
            await second.threads().request_cancel(
                ThreadKey(system="generic", thread="t")
            )
            await asyncio.sleep(0.2)  # опрос владельца видит отмену
            return THINK
        return scripted_model.FINISH

    seen = _install(monkeypatch, plan)
    try:
        async with _client(first) as a, _client(second) as b:
            await a.post("/events/webhook", json={"task_id": "t", "x": 1})
            done = await _wait(b, "/runs/generic/t", "cancelled")
    finally:
        await first.astop()
        await second.astop()
        await stores.aclose()
    assert done["finished_at"] is not None
    assert len(seen) == 1
