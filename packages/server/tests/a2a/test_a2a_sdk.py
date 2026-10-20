"""Поверхность a2a на ``a2a-sdk``: проверка клиентом SDK.

Задачи лежат в хранилище тредов (SQLite): их видит любой инстанс. Агент —
двойник с управляемым ходом (вопрос пиру, удержание), а один сквозной тест —
настоящий агент на скриптованной модели.
"""

import asyncio
import sys
import uuid
from types import SimpleNamespace

import httpx
import pytest
from a2a.client import ClientConfig, ClientFactory
from a2a.types import a2a_pb2 as pb
from gc_server.a2a.executor import BUSY_TEXT
from gc_server.a2a.surface import A2aSurface
from gc_server.app import ServerRuntime
from gc_server.config import ServerConfig, ServerConfigError
from gc_server.stores import aopen_process_stores
from gc_testkit import scripted_model
from gc_testkit.settings import make_settings

BASE = "http://127.0.0.1:8091"


class FakeAgent:
    """Ход по сценарию: ``("ask", вопрос)`` — вопрос пиру, ``("hold",
    событие)`` — ждать события."""

    def __init__(self, *script, answer="готово"):
        self.script = list(script)
        self.answer = answer
        self.calls: list = []
        self.answers: list = []
        self.stopped = asyncio.Event()
        self.settings = None
        self.executor = SimpleNamespace(
            _ctx=SimpleNamespace(), last_terminal_outcome=None
        )
        self.last_answer = None

    async def stream(self, prompt, thread_id=None, system=None):
        self.calls.append((prompt, thread_id, system))
        for kind, value in self.script:
            if kind == "ask":
                self.answers.append(await self.executor._ctx.ask_human(value))
            elif kind == "hold":
                stopped = asyncio.create_task(self.stopped.wait())
                held = asyncio.create_task(value.wait())
                await asyncio.wait(
                    {stopped, held}, return_when=asyncio.FIRST_COMPLETED
                )
                stopped.cancel()
                held.cancel()
            yield ("answer", "кусок")
        self.last_answer = self.answer

    def stop(self):
        self.stopped.set()

    async def aclose(self):
        return None


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


#: Поверхности теста: их ходы и задачи SDK гасятся до закрытия хранилища —
#: операция с закрытой SQLite не завершается, и цикл событий не закрылся бы.
_LIVE: list = []


@pytest.fixture()
async def stores(tmp_path):
    opened = await aopen_process_stores(_settings(tmp_path))
    yield opened
    while _LIVE:
        await _LIVE.pop().astop()
    await asyncio.sleep(0.05)
    await opened.aclose()


async def _allow_any(_url):
    return True


def _surface(tmp_path, stores, agents=None, *, section=None, push=None):
    runtime = ServerRuntime(
        base=_settings(tmp_path),
        config=ServerConfig.model_validate({"a2a": section or {}}),
        stores=stores,
    )
    surface = A2aSurface(
        runtime, push_client=push, push_url_validator=_allow_any
    )
    if agents is not None:

        async def factory(settings, **_kw):
            agent = agents.pop(0)
            agent.settings = settings
            return agent

        surface.executor._agent_factory = factory
    surface.http_app = surface.app()
    _LIVE.append(surface)
    return surface


def _client(surface, *, streaming=False):
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=surface.http_app), base_url=BASE
    )
    return ClientFactory(
        ClientConfig(httpx_client=http, streaming=streaming)
    ).create(surface.card())


def _message(text, **fields):
    return pb.Message(
        role=pb.ROLE_USER,
        message_id=str(uuid.uuid4()),
        parts=[pb.Part(text=text)],
        **fields,
    )


async def _send(client, message, **configuration):
    request = pb.SendMessageRequest(message=message)
    for name, value in configuration.items():
        setattr(request.configuration, name, value)
    last = None
    async for event in client.send_message(request):
        last = event
    return last.task


def _text(message) -> str:
    return "".join(part.text for part in message.parts)


async def test_card_declares_streaming_push_and_public_url(tmp_path, stores):
    surface = _surface(
        tmp_path,
        stores,
        section={
            "public_url": "https://agent.example/rpc",
            "push_allowed_hosts": ["hooks.example"],
        },
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=surface.http_app), base_url=BASE
    ) as http:
        card = (await http.get("/.well-known/agent-card.json")).json()
    assert card["capabilities"]["streaming"] is True
    assert card["capabilities"]["pushNotifications"] is True
    urls = {
        i["protocolBinding"]: i["url"] for i in card["supportedInterfaces"]
    }
    assert urls == {
        "JSONRPC": "https://agent.example/rpc",
        "HTTP+JSON": "https://agent.example",
    }


async def test_push_is_off_without_allowed_hosts(tmp_path, stores):
    """Адрес push задаёт пир — сервер ходил бы по нему. Без явного списка
    хостов push выключен: в карточке его нет, настройка в запросе не
    действует — наружу ничего не уходит."""
    sent: list = []
    push = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: sent.append(r) or httpx.Response(200)
        )
    )
    surface = _surface(tmp_path, stores, [FakeAgent()], push=push)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=surface.http_app), base_url=BASE
    ) as http:
        card = (await http.get("/.well-known/agent-card.json")).json()
    assert card["capabilities"].get("pushNotifications") is not True
    request = pb.SendMessageRequest(message=_message("сделай"))
    request.configuration.task_push_notification_config.url = (
        "https://hooks.example/a2a"
    )
    async for _event in _client(surface).send_message(request):
        pass
    await asyncio.sleep(0.05)
    assert sent == []


async def test_push_to_a_host_outside_the_list_is_refused(tmp_path, stores):
    sent: list = []
    push = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: sent.append(r) or httpx.Response(200)
        )
    )
    surface = _surface(
        tmp_path,
        stores,
        [FakeAgent()],
        section={"push_allowed_hosts": ["hooks.example"]},
        push=push,
    )
    request = pb.SendMessageRequest(message=_message("сделай"))
    request.configuration.task_push_notification_config.url = (
        "https://internal.corp/steal"
    )
    with pytest.raises(Exception):
        async for _event in _client(surface).send_message(request):
            pass
    assert sent == []


async def test_send_message_completes_with_the_answer(tmp_path, stores):
    agent = FakeAgent()
    surface = _surface(tmp_path, stores, [agent])
    task = await _send(_client(surface), _message("сделай"))
    assert task.status.state == pb.TASK_STATE_COMPLETED
    assert _text(task.artifacts[0]) == "готово"
    assert agent.calls == [("сделай", task.context_id, "a2a")]


async def test_metadata_cwd_is_the_turn_root(tmp_path, stores):
    root = tmp_path / "root"
    (root / "proj").mkdir(parents=True)
    agent = FakeAgent()
    surface = _surface(
        tmp_path,
        stores,
        [agent, FakeAgent(), FakeAgent()],
        section={"launch_root": str(root)},
    )
    message = _message("сделай")
    message.metadata.update({"cwd": str(root / "proj")})
    task = await _send(_client(surface), message)
    assert task.status.state == pb.TASK_STATE_COMPLETED
    assert agent.settings.runtime.launch_root == str(root / "proj")

    relative = _message("сделай")
    relative.metadata.update({"cwd": "relative/dir"})
    failed = await _send(_client(surface), relative)
    assert failed.status.state == pb.TASK_STATE_FAILED


@pytest.mark.parametrize("outside", ["/", "/etc", "{root}/../elsewhere"])
async def test_peer_cannot_root_the_turn_outside_the_surface_root(
    tmp_path, stores, outside
):
    """Корень хода — периметр записи: пир выбирает его только внутри
    a2a.launch_root (без него — внутри каталога процесса)."""
    root = tmp_path / "root"
    root.mkdir()
    surface = _surface(
        tmp_path, stores, [FakeAgent()], section={"launch_root": str(root)}
    )
    message = _message("сделай")
    message.metadata.update({"cwd": outside.format(root=root)})
    task = await _send(_client(surface), message)
    assert task.status.state == pb.TASK_STATE_FAILED


async def test_question_then_reply_continues_the_same_task(tmp_path, stores):
    agent = FakeAgent(("ask", "какой цвет?"))
    surface = _surface(tmp_path, stores, [agent])
    client = _client(surface)
    asked = await _send(client, _message("покрась"))
    assert asked.status.state == pb.TASK_STATE_INPUT_REQUIRED
    assert _text(asked.status.message) == "какой цвет?"
    done = await _send(client, _message("синий", task_id=asked.id))
    assert done.id == asked.id
    assert done.status.state == pb.TASK_STATE_COMPLETED
    assert agent.answers == ["синий"]


async def test_task_is_visible_from_another_instance(tmp_path, stores):
    first = _surface(tmp_path, stores, [FakeAgent()])
    second = _surface(tmp_path, stores, [])
    task = await _send(_client(first), _message("сделай"))
    seen = await _client(second).get_task(pb.GetTaskRequest(id=task.id))
    assert seen.status.state == pb.TASK_STATE_COMPLETED
    listed = await _client(second).list_tasks(pb.ListTasksRequest())
    assert [t.id for t in listed.tasks] == [task.id]


async def test_cancel_task_stops_the_turn(tmp_path, stores):
    agent = FakeAgent(("hold", asyncio.Event()))
    surface = _surface(tmp_path, stores, [agent])
    client = _client(surface)
    task = await _send(client, _message("долго"), return_immediately=True)
    for _ in range(200):
        if agent.calls:
            break
        await asyncio.sleep(0.01)
    canceled = await client.cancel_task(pb.CancelTaskRequest(id=task.id))
    assert canceled.status.state == pb.TASK_STATE_CANCELED
    assert agent.stopped.is_set()


async def test_streaming_reports_status_events(tmp_path, stores):
    surface = _surface(tmp_path, stores, [FakeAgent()])
    client = _client(surface, streaming=True)
    states = []
    request = pb.SendMessageRequest(message=_message("сделай"))
    async for event in client.send_message(request):
        if event.HasField("status_update"):
            states.append(event.status_update.status.state)
    assert pb.TASK_STATE_WORKING in states
    assert states[-1] == pb.TASK_STATE_COMPLETED


async def test_push_notification_reaches_the_receiver(tmp_path, stores):
    received: list = []

    def receiver(request: httpx.Request) -> httpx.Response:
        received.append((str(request.url), request.headers, request.content))
        return httpx.Response(200)

    push = httpx.AsyncClient(transport=httpx.MockTransport(receiver))
    surface = _surface(
        tmp_path,
        stores,
        [FakeAgent()],
        section={"push_allowed_hosts": ["hooks.example"]},
        push=push,
    )
    request = pb.SendMessageRequest(message=_message("сделай"))
    request.configuration.task_push_notification_config.url = (
        "https://hooks.example/a2a"
    )
    request.configuration.task_push_notification_config.token = "secret"
    async for event in _client(surface).send_message(request):
        task = event.task
    for _ in range(200):
        if received:
            break
        await asyncio.sleep(0.01)
    assert received and received[0][0] == "https://hooks.example/a2a"
    configs = await _client(surface).list_task_push_notification_configs(
        pb.ListTaskPushNotificationConfigsRequest(task_id=task.id)
    )
    assert [c.url for c in configs.configs] == ["https://hooks.example/a2a"]


async def test_busy_context_fails_the_new_task(tmp_path, stores):
    hold = asyncio.Event()
    surface = _surface(tmp_path, stores, [FakeAgent(("hold", hold))])
    client = _client(surface)
    first = await _send(client, _message("долго"), return_immediately=True)
    second = await _send(client, _message("ещё", context_id=first.context_id))
    assert second.status.state == pb.TASK_STATE_FAILED
    assert _text(second.status.message) == BUSY_TEXT
    hold.set()
    for _ in range(200):
        done = await client.get_task(pb.GetTaskRequest(id=first.id))
        if done.status.state == pb.TASK_STATE_COMPLETED:
            break
        await asyncio.sleep(0.01)
    assert done.status.state == pb.TASK_STATE_COMPLETED


async def test_real_agent_answers_through_a2a(tmp_path, stores, monkeypatch):
    seen = scripted_model.install(monkeypatch)
    surface = _surface(tmp_path, stores)
    task = await _send(_client(surface), _message("сделай"))
    assert task.status.state == pb.TASK_STATE_COMPLETED
    assert _text(task.artifacts[0]) == "готово"
    assert len(seen) == 1


def test_without_the_sdk_the_surface_refuses(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "a2a.server.request_handlers", None)
    runtime = ServerRuntime(base=_settings(tmp_path), config=ServerConfig())
    with pytest.raises(ServerConfigError) as err:
        A2aSurface(runtime)
    assert "gc-server[a2a]" in str(err.value)
