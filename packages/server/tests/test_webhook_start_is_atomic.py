"""Два вебхука на одну задачу: ключ занят с первого запроса, сообщение не теряется.

Сборка контекста идёт через `await` (дискавери MCP по сети). Пока она идёт,
ключ задачи занят бронью: второй вебхук не строит второй контекст. Его
сообщение уже во входящем потоке треда, поэтому он сразу отвечает `queued` —
прогон первого заберёт его в начале хода. Упала сборка — сообщения остаются
в потоке: следующий вебхук задачи строит прогон и доставляет их все.

Всё проверяется через настоящий эндпоинт, реестр и поток треда. Подменены
только сборка контекста (её держит заслонка теста) и сам агент.
"""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from gc_core.threads.models import ThreadKey
from gc_server.webhook import surface as server
from server_harness import LOOPBACK_URL, make_webhook

KEY = ThreadKey(system="s", thread="t")
#: Потолок любого ожидания: сломанная бронь даёт красный тест, а не вечный сон.
_BOUND_SEC = 5.0


def _within(awaitable):
    return asyncio.wait_for(awaitable, _BOUND_SEC)


class _Agent:
    """Двойник AgentExecutor: ход читает входящий поток треда — в начале и
    после ``hold`` (как опрос настоящего)."""

    def __init__(self, threads, ctx):
        self.threads = threads
        self.ctx = ctx
        self.hold = getattr(ctx, "hold", None)
        self.delivered: list = []
        self._seen = 0

    async def _pick_up(self) -> None:
        entries = await self.threads.store.read_stream(
            KEY, "inbox", after_seq=self._seen
        )
        for entry in entries:
            self.delivered.append(json.loads(entry.data["content"])["text"])
            self._seen = entry.seq

    async def arun(self, messages, **kwargs):
        assert messages == []  # ход вебхука — из входящего потока
        # Как настоящий исполнитель: тред взят — сказать поверхности.
        await self.ctx.on_thread_claimed(KEY.system, KEY.thread)
        await self._pick_up()
        if self.hold is not None:
            await self.hold.wait()
        await self._pick_up()
        return "done"

    async def ainbox_left(self) -> bool:
        return False

    def stop_run(self) -> None:
        pass


@pytest.fixture()
def harness(monkeypatch, tmp_path):
    """Управляемая сборка контекста и двойник агента.

    Каждая сборка берёт следующий шаг из ``plan``: ``entered`` — выставить
    при входе, ``gate`` — ждать перед выходом, ``fail`` — упасть, ``hold`` —
    держать ход агента открытым до выставления.
    """
    surface = make_webhook(tmp_path)
    h = SimpleNamespace(surface=surface, plan=[], builds=0, agents=[])

    async def _build(loaded, **kwargs):
        step = h.plan.pop(0) if h.plan else {}
        h.builds += 1
        if step.get("entered") is not None:
            step["entered"].set()
        if step.get("gate") is not None:
            await step["gate"].wait()
        if step.get("fail"):
            raise RuntimeError("MCP-сервер не ответил")
        return SimpleNamespace(hold=step.get("hold"), logger=None)

    def _executor(ctx):
        agent = _Agent(surface.threads(), ctx)
        h.agents.append(agent)
        return agent

    monkeypatch.setattr(server, "acreate_run_context", _build)
    monkeypatch.setattr(server, "AgentExecutor", _executor)
    monkeypatch.setattr(server, "build_auth_connector", lambda cfg: None)
    return h


def _client(surface) -> httpx.AsyncClient:
    # Упавший эндпоинт даёт 500, как у uvicorn, а не исключение в тесте.
    transport = httpx.ASGITransport(
        app=surface.app(), raise_app_exceptions=False
    )
    return httpx.AsyncClient(transport=transport, base_url=LOOPBACK_URL)


async def _post(client: httpx.AsyncClient, text: str) -> httpx.Response:
    return await client.post(
        "/v1/tasks", json={"task_id": "t", "system": "s", "text": text}
    )


async def _settle(surface) -> None:
    """Дождаться конца своих прогонов задачи."""
    tasks = surface.registry.live_tasks()
    if tasks:
        await _within(asyncio.gather(*tasks, return_exceptions=True))


async def test_second_webhook_during_the_build_is_queued_and_delivered(
    harness,
):
    gate, entered = asyncio.Event(), asyncio.Event()
    harness.plan[:] = [{"entered": entered, "gate": gate}]
    async with _client(harness.surface) as client:
        first = asyncio.create_task(_post(client, "первый"))
        await _within(entered.wait())
        second = await _within(_post(client, "второй"))
        assert second.json()["status"] == "queued"
        gate.set()
        assert (await _within(first)).json()["status"] == "started"
        await _settle(harness.surface)
    assert harness.builds == 1
    assert harness.agents[0].delivered == ["первый", "второй"]


async def test_failed_build_keeps_messages_for_the_next_webhook(harness):
    """Сборка первого упала: его сообщение и сообщение ждавшего не пропали —
    следующий вебхук задачи строит прогон и доставляет все три."""
    gate, entered = asyncio.Event(), asyncio.Event()
    harness.plan[:] = [{"entered": entered, "gate": gate, "fail": True}, {}]
    async with _client(harness.surface) as client:
        first = asyncio.create_task(_post(client, "первый"))
        await _within(entered.wait())
        second = await _within(_post(client, "второй"))
        assert second.json()["status"] == "queued"
        gate.set()
        assert (await _within(first)).status_code == 500
        third = await _within(_post(client, "третий"))
        assert third.json()["status"] == "started"
        await _settle(harness.surface)
    assert harness.agents[-1].delivered == ["первый", "второй", "третий"]


async def test_failed_build_releases_the_key(harness):
    harness.plan[:] = [{"fail": True}]
    async with _client(harness.surface) as client:
        assert (await _within(_post(client, "первый"))).status_code == 500
    registry = harness.surface.registry
    assert registry.building_since(system="s", task_id="t") is None
    assert not registry.is_running(system="s", task_id="t")
    assert registry.reserve(system="s", task_id="t")


async def test_status_during_the_build_is_running_not_the_previous_result(
    harness,
):
    """Бронь проверяется раньше статуса в хранилище: там — итог прошлого
    прогона, а клиенту только что ответили про новый."""
    async with _client(harness.surface) as client:
        await _within(_post(client, "первый"))
        await _settle(harness.surface)
        done = (await client.get("/v1/tasks/s/t")).json()
        assert done["status"] == "completed"

        gate, entered = asyncio.Event(), asyncio.Event()
        harness.plan[:] = [{"entered": entered, "gate": gate}]
        second = asyncio.create_task(_post(client, "второй"))
        await _within(entered.wait())
        building = (await client.get("/v1/tasks/s/t")).json()
        assert building["status"] == "running"
        assert "result" not in building
        assert building["started_at"] == building["last_activity_at"]
        assert building["started_at"] >= done["finished_at"]
        gate.set()
        await _within(second)
        await _settle(harness.surface)
