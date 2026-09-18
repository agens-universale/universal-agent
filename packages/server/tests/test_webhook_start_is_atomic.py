"""Два вебхука на одну задачу: ключ занят с первого запроса, сообщение не теряется.

Сборка контекста идёт через `await` (дискавери MCP по сети). Раньше проверка
«идёт ли прогон» и старт стояли по разные стороны этого ожидания: второй вебхук
видел ключ свободным, строил ВТОРОЙ контекст, оба запроса отвечали `started`, а
сообщение второго не доходило ни до кого. Отсюда бронь ключа до первого `await`.

Вторая редакция брони копила сообщения ждущих у себя и при отказе сборки
откладывала их до «следующего вебхука той же задачи». Следующего могло не быть
вовсе: сообщение, которому ответили `queued`, не доходило никогда, а клиент
видел 404. Теперь бронь хранит только событие «сборка закончилась», и ждущий
вебхук проходит путь заново: прогон жив — `queued`, прогона нет — строит сам.

Всё проверяется через настоящий эндпоинт и настоящий реестр. Подменены только
сборка контекста (её держит заслонка теста) и сам агент.
"""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from gc_server import server
from gc_server.rest_api.agent_execution_registry import AgentRunRegistry

#: Потолок любого ожидания в тестах. Сломанная бронь даёт не красный тест, а
#: вечный сон ждущего вебхука — без потолка набор просто зависнет.
_BOUND_SEC = 5.0


def _within(awaitable):
    return asyncio.wait_for(awaitable, _BOUND_SEC)


class _Agent:
    """Двойник AgentExecutor с семантикой очереди настоящего.

    Стартовые сообщения — первым батчем; что легло в очередь до хода и во время
    него, ход забирает сам; что легло после последнего забора — хвост, его
    отдаёт `take_undelivered_messages`. В `delivered` — всё, что дошло до
    «модели», в порядке доставки.
    """

    def __init__(self, ctx):
        self.hold = getattr(ctx, "hold", None)
        self.delivered: list = []
        self._queue: list = []

    def add_message(self, message) -> None:
        self._queue.append(message)

    def take_undelivered_messages(self) -> list:
        out, self._queue = self._queue, []
        return out

    def stop_run(self) -> None:
        pass

    def _pick_up(self) -> None:
        self.delivered.extend(m["content"] for m in self._queue)
        self._queue = []

    async def arun(self, messages, **kwargs):
        self.delivered.extend(messages)
        self._pick_up()
        if self.hold is not None:
            await self.hold.wait()
        self._pick_up()
        return "done"


@pytest.fixture()
def harness(monkeypatch):
    """Свой реестр, управляемая сборка контекста и двойник агента.

    Каждая сборка берёт следующий шаг из `plan`: `entered` — выставить при
    входе, `gate` — ждать перед выходом, `fail` — упасть, `hold` — держать ход
    агента открытым до выставления.
    """
    registry = AgentRunRegistry()
    h = SimpleNamespace(registry=registry, plan=[], builds=0, agents=[])

    async def _build(config_dict, **kwargs):
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
        agent = _Agent(ctx)
        h.agents.append(agent)
        return agent

    monkeypatch.setattr(server, "agent_run_registry", registry)
    monkeypatch.setattr(server, "acreate_run_context", _build)
    monkeypatch.setattr(server, "AgentExecutor", _executor)
    monkeypatch.setattr(server, "build_auth_connector", lambda cfg: None)
    return h


def _client() -> httpx.AsyncClient:
    # Запросы идут в приложение на loop теста: два вебхука живут одновременно.
    # Упавший эндпоинт даёт 500, как у uvicorn, а не исключение в тесте.
    transport = httpx.ASGITransport(app=server.app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _post(client: httpx.AsyncClient, text: str) -> httpx.Response:
    return await client.post(
        "/events/webhook", json={"task_id": "t", "system": "s", "text": text}
    )


def _texts(delivered: list) -> list:
    return [json.loads(content)["text"] for content in delivered]


def _watch_arrival(monkeypatch, registry: AgentRunRegistry, text: str) -> asyncio.Event:
    """Событие «вебхук с этим текстом дошёл до реестра».

    Дальше до ожидания брони у эндпоинта нет ни одного `await`, поэтому к
    моменту, когда тест проснётся по этому событию, вебхук уже ждёт.
    """
    arrived = asyncio.Event()
    original = registry.add_user_message

    def _spy(**kwargs):
        if json.loads(kwargs["content"]).get("text") == text:
            arrived.set()
        return original(**kwargs)

    monkeypatch.setattr(registry, "add_user_message", _spy)
    return arrived


async def _settle(registry: AgentRunRegistry) -> None:
    """Дождаться конца прогона задачи: его таска лежит в поле `thread`."""
    run = registry.get_status(system="s", task_id="t")
    if run is not None and run.thread is not None:
        await _within(run.thread)


async def _failed_build_with_a_waiter(harness, monkeypatch):
    """Первый вебхук строит контекст, второй ждёт, сборка первого падает."""
    gate, entered = asyncio.Event(), asyncio.Event()
    harness.plan[:] = [{"entered": entered, "gate": gate, "fail": True}, {}]
    second_arrived = _watch_arrival(monkeypatch, harness.registry, "второй")

    async with _client() as client:
        first = asyncio.create_task(_post(client, "первый"))
        await _within(entered.wait())
        second = asyncio.create_task(_post(client, "второй"))
        await _within(second_arrived.wait())
        gate.set()
        return await _within(asyncio.gather(first, second))


async def test_waiting_webhook_builds_its_own_context_when_the_first_build_fails(
    harness, monkeypatch
):
    first, second = await _failed_build_with_a_waiter(harness, monkeypatch)

    assert first.status_code == 500, "упавшая сборка обязана честно ответить ошибкой"
    assert second.status_code == 200
    assert second.json()["status"] == "started", (
        f"второй ответил {second.json()['status']!r}: его сообщение ждёт вебхука, "
        f"которого может не быть, вместо того чтобы стартовать прогон самому"
    )
    await _settle(harness.registry)
    assert len(harness.agents) == 1
    assert _texts(harness.agents[0].delivered) == ["второй"], (
        "сообщение второго вебхука не дошло до агента"
    )


async def test_waiting_webhook_joins_the_run_after_a_successful_build(
    harness, monkeypatch
):
    gate, entered, hold = asyncio.Event(), asyncio.Event(), asyncio.Event()
    harness.plan[:] = [{"entered": entered, "gate": gate, "hold": hold}]
    second_arrived = _watch_arrival(monkeypatch, harness.registry, "второй")

    async with _client() as client:
        first = asyncio.create_task(_post(client, "первый"))
        await _within(entered.wait())
        second = asyncio.create_task(_post(client, "второй"))
        await _within(second_arrived.wait())
        gate.set()
        first, second = await _within(asyncio.gather(first, second))

    assert [first.json()["status"], second.json()["status"]] == ["started", "queued"]
    assert harness.builds == 1, "построен второй контекст впустую: его никто не закроет"
    hold.set()
    await _settle(harness.registry)
    assert _texts(harness.agents[0].delivered) == ["первый", "второй"], (
        "сообщения дошли до агента не в порядке прихода"
    )


async def test_nothing_is_left_behind_after_a_failed_build(harness, monkeypatch):
    """После отказа сборки в реестре не остаётся ни брони, ни отложенного.

    Раньше сообщение ждущего оставалось в реестре без срока и уезжало в
    следующий прогон задачи — позади сообщения, пришедшего позже него.
    """
    await _failed_build_with_a_waiter(harness, monkeypatch)
    await _settle(harness.registry)

    assert not harness.registry._reservations, "бронь пережила свою сборку"
    async with _client() as client:
        third = await _within(_post(client, "третий"))
    assert third.json()["status"] == "started"
    await _settle(harness.registry)
    assert _texts(harness.agents[-1].delivered) == ["третий"], (
        "в новый прогон приехало сообщение, оставшееся от прошлых запросов"
    )


def test_failed_context_build_releases_the_key():
    """Бронь снята — ключ снова можно занять, иначе задача «строится» вечно."""
    registry = AgentRunRegistry()

    assert registry.reserve(system="s", task_id="t") is True
    assert registry.reserve(system="s", task_id="t") is False

    registry.release_reservation(system="s", task_id="t")

    assert registry.reserve(system="s", task_id="t") is True

# --- статус во время сборки -------------------------------------------------


async def test_status_during_the_build_is_running_not_the_previous_result(harness):
    """Пока строится контекст, статус — `running`, а не итог прошлого прогона.

    Бронь выдаётся ровно тогда, когда прошлый прогон закончился, а его запись
    живёт в `_completed` ещё 15 минут. Клиент, которому только что ответили
    `queued`, видел `completed` и `result` ЧУЖОГО прогона.
    """
    registry = harness.registry
    previous = await registry.astart_if_absent(
        system="s", task_id="t", executor_factory=_Agent,
        ctx=SimpleNamespace(logger=None), initial_user_message="прошлый",
    )
    await _within(previous.thread)

    async with _client() as client:
        assert (await client.get("/runs/s/t")).json()["status"] == "completed"
        assert registry.reserve(system="s", task_id="t") is True
        during = (await client.get("/runs/s/t")).json()
        registry.release_reservation(system="s", task_id="t")
        after = (await client.get("/runs/s/t")).json()

    assert during["status"] == "running", (
        f"во время сборки статус {during['status']!r} — это итог прошлого прогона"
    )
    assert "result" not in during
    assert after["status"] == "completed"
