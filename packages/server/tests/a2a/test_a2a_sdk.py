"""Поверхность a2a на ``a2a-sdk``: проверка клиентом SDK.

Задачи лежат в хранилище тредов (SQLite): их видит любой инстанс. Агент —
двойник с управляемым ходом (вопрос пиру — приостановка хода, удержание), а
один сквозной тест — настоящий агент на скриптованной модели.
"""

import asyncio
import os
import sys
import uuid
from types import SimpleNamespace

import httpx
import pytest
from a2a.client import ClientConfig, ClientFactory
from a2a.types import a2a_pb2 as pb
from gc_server.a2a import executor as a2a_executor
from gc_server.a2a.executor import INTERRUPTED_TEXT
from gc_server.a2a.task_store import TASKS_STREAM, dialog_key
from gc_server.a2a.surface import A2aSurface
from gc_server.app import ServerRuntime
from gc_server.config import ServerConfig, ServerConfigError
from gc_server.stores import aopen_process_stores
from gc_testkit import scripted_model
from gc_testkit.settings import make_settings

BASE = "http://127.0.0.1:8091"


class FakeAgent:
    """Ход по сценарию: ``("ask", вопрос)`` — вопрос пиру (ход
    приостанавливается, как с ``ask_human_suspends``; ответ — следующим
    ходом с ``answer=``), ``("hold", событие)`` — ждать события."""

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
        self.pending_question = None
        self._step = 0

    async def stream(
        self, prompt, thread_id=None, system=None, answer=None, **_kw
    ):
        self.calls.append((prompt, thread_id, system))
        if answer is not None:
            self.answers.append(answer)
        self.pending_question = None
        while self._step < len(self.script):
            kind, value = self.script[self._step]
            self._step += 1
            if kind == "ask":
                self.pending_question = {"call_id": "q", "question": value}
                yield ("answer", "кусок")
                return
            if kind == "hold":
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


@pytest.fixture(autouse=True)
def _fast_queue(monkeypatch):
    monkeypatch.setattr(a2a_executor, "QUEUE_POLL_SECONDS", 0.02)


async def _until(client, task_id, state):
    for _ in range(400):
        task = await client.get_task(pb.GetTaskRequest(id=task_id))
        if task.status.state == state:
            return task
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"задача {task_id}: {task.status.state}, ждали {state}"
    )


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


async def test_new_task_waits_its_turn_in_the_dialog(tmp_path, stores):
    """Диалог ведёт задачи по очереди: новая задача в занятом диалоге —
    ``SUBMITTED`` и стартует, когда предыдущая закончилась."""
    hold = asyncio.Event()
    agent = FakeAgent(("hold", hold))
    surface = _surface(tmp_path, stores, [agent])
    client = _client(surface)
    first = await _send(client, _message("долго"), return_immediately=True)
    second = await _send(
        client,
        _message("ещё", context_id=first.context_id),
        return_immediately=True,
    )
    assert second.status.state == pb.TASK_STATE_SUBMITTED
    hold.set()
    await _until(client, first.id, pb.TASK_STATE_COMPLETED)
    await _until(client, second.id, pb.TASK_STATE_COMPLETED)
    assert [call[0] for call in agent.calls] == ["долго", "ещё"]


async def test_question_holds_the_dialog_until_answered(tmp_path, stores):
    """Задача, ждущая ответа, держит очередь диалога: новая задача стартует
    после ответа (а не перебивает вопрос)."""
    agent = FakeAgent(("ask", "какой цвет?"))
    surface = _surface(tmp_path, stores, [agent])
    client = _client(surface)
    asked = await _send(client, _message("покрась"))
    assert asked.status.state == pb.TASK_STATE_INPUT_REQUIRED
    queued = await _send(
        client,
        _message("и ещё", context_id=asked.context_id),
        return_immediately=True,
    )
    await asyncio.sleep(0.1)
    still = await client.get_task(pb.GetTaskRequest(id=queued.id))
    assert still.status.state == pb.TASK_STATE_SUBMITTED
    done = await _send(client, _message("синий", task_id=asked.id))
    assert done.status.state == pb.TASK_STATE_COMPLETED
    await _until(client, queued.id, pb.TASK_STATE_COMPLETED)
    assert agent.answers == ["синий"]


async def test_answer_continues_the_task_on_another_instance(tmp_path, stores):
    """Ход не ждёт ответа в памяти инстанса: ответ, пришедший на другой
    инстанс, продолжает задачу там."""
    first = _surface(tmp_path, stores, [FakeAgent(("ask", "какой цвет?"))])
    other_agent = FakeAgent()
    second = _surface(tmp_path, stores, [other_agent])
    asked = await _send(_client(first), _message("покрась"))
    assert asked.status.state == pb.TASK_STATE_INPUT_REQUIRED
    # Продолжая задачу, пир называет её диалог (contextId): другой инстанс
    # её в памяти не держит.
    done = await _send(
        _client(second),
        _message("синий", task_id=asked.id, context_id=asked.context_id),
    )
    assert done.status.state == pb.TASK_STATE_COMPLETED
    assert other_agent.answers == ["синий"]
    assert other_agent.calls[0][0] == []


async def test_cancel_removes_the_task_from_the_queue(tmp_path, stores):
    hold = asyncio.Event()
    agent = FakeAgent(("hold", hold))
    surface = _surface(tmp_path, stores, [agent])
    client = _client(surface)
    first = await _send(client, _message("долго"), return_immediately=True)
    queued = await _send(
        client,
        _message("ещё", context_id=first.context_id),
        return_immediately=True,
    )
    canceled = await client.cancel_task(pb.CancelTaskRequest(id=queued.id))
    assert canceled.status.state == pb.TASK_STATE_CANCELED
    hold.set()
    await _until(client, first.id, pb.TASK_STATE_COMPLETED)
    await asyncio.sleep(0.1)
    assert [call[0] for call in agent.calls] == ["долго"]


async def test_canceling_a_waiting_task_frees_the_dialog(tmp_path, stores):
    agent = FakeAgent(("ask", "какой цвет?"))
    surface = _surface(tmp_path, stores, [agent])
    client = _client(surface)
    asked = await _send(client, _message("покрась"))
    queued = await _send(
        client,
        _message("и ещё", context_id=asked.context_id),
        return_immediately=True,
    )
    canceled = await client.cancel_task(pb.CancelTaskRequest(id=asked.id))
    assert canceled.status.state == pb.TASK_STATE_CANCELED
    await _until(client, queued.id, pb.TASK_STATE_COMPLETED)


def _fast_owners(*surfaces) -> None:
    """Реестр владельца с коротким сроком: смерть видна за доли секунды."""
    from datetime import timedelta

    for surface in surfaces:
        surface.executor._owners._ttl = timedelta(seconds=0.3)
        surface.executor._owners._renew_seconds = 0.1


async def _crash(surface) -> None:
    """Инстанс умер: продление реестра остановилось, аренда не отпущена."""
    keeper = surface.executor._owners._keeper
    if keeper is not None:
        keeper.cancel()
    await asyncio.sleep(0.45)


async def test_recovery_fails_the_task_of_a_dead_instance(tmp_path, stores):
    """Ход на инстансе, который умер (аренда его реестра истекла), —
    задача ``FAILED`` с причиной; диалог свободен для очереди."""
    dying = _surface(tmp_path, stores, [FakeAgent(("hold", asyncio.Event()))])
    survivor = _surface(tmp_path, stores, [])
    _fast_owners(dying, survivor)
    task = await _send(
        _client(dying), _message("долго"), return_immediately=True
    )
    await _until(_client(dying), task.id, pb.TASK_STATE_WORKING)
    await _crash(dying)
    assert await survivor.executor.arecover_once() == 1
    failed = await _client(survivor).get_task(pb.GetTaskRequest(id=task.id))
    assert failed.status.state == pb.TASK_STATE_FAILED
    assert _text(failed.status.message) == INTERRUPTED_TEXT


async def test_task_snapshots_live_in_the_dialog_stream(tmp_path, stores):
    """Задача — снимки в потоке ``a2a.tasks``; в состоянии фронтенда —
    только номер последнего снимка."""
    surface = _surface(tmp_path, stores, [FakeAgent()])
    task = await _send(_client(surface), _message("сделай"))
    key = dialog_key(task.context_id)
    snapshots = await stores.threads.read_stream(key, TASKS_STREAM)
    assert len(snapshots) >= 2
    record = await stores.threads.get_thread(key)
    index = record.frontend["a2a"]["task:" + task.id]
    assert index == snapshots[-1].seq


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


async def test_turn_root_is_the_resolved_path(tmp_path, stores):
    """Ходу отдаётся раскрытый путь: подмена компонента симлинком после
    проверки не уведёт корень хода за периметр."""
    root = tmp_path / "root"
    (root / "proj").mkdir(parents=True)
    (root / "alias").symlink_to(root / "proj")
    agent = FakeAgent()
    surface = _surface(
        tmp_path, stores, [agent], section={"launch_root": str(root)}
    )
    message = _message("сделай")
    message.metadata.update({"cwd": str(root / "alias")})
    task = await _send(_client(surface), message)
    assert task.status.state == pb.TASK_STATE_COMPLETED
    assert agent.settings.runtime.launch_root == os.path.realpath(
        root / "proj"
    )


async def test_cancel_on_another_instance_frees_a_waiting_dialog(
    tmp_path, stores
):
    """Отмена задачи, ждущей ответа, через другой инстанс (SDK пишет
    ``CANCELED`` сам, мимо исполнителя): диалог свободен, очередь идёт."""
    asker = FakeAgent(("ask", "какой цвет?"))
    first = _surface(tmp_path, stores, [asker])
    second = _surface(tmp_path, stores, [])
    asked = await _send(_client(first), _message("покрась"))
    queued = await _send(
        _client(first),
        _message("и ещё", context_id=asked.context_id),
        return_immediately=True,
    )
    canceled = await _client(second).cancel_task(
        pb.CancelTaskRequest(id=asked.id)
    )
    assert canceled.status.state == pb.TASK_STATE_CANCELED
    await _until(_client(first), queued.id, pb.TASK_STATE_COMPLETED)


async def test_cancel_after_restart_frees_a_waiting_dialog(tmp_path, stores):
    """Вопрос пережил рестарт, задачу отменили на новом процессе: новая
    задача диалога идёт, а не висит в очереди вечно."""
    first = _surface(tmp_path, stores, [FakeAgent(("ask", "какой цвет?"))])
    asked = await _send(_client(first), _message("покрась"))
    await first.astop()
    restarted = _surface(tmp_path, stores, [FakeAgent()])
    client = _client(restarted)
    canceled = await client.cancel_task(pb.CancelTaskRequest(id=asked.id))
    assert canceled.status.state == pb.TASK_STATE_CANCELED
    later = await _send(
        client,
        _message("новое", context_id=asked.context_id),
        return_immediately=True,
    )
    await _until(client, later.id, pb.TASK_STATE_COMPLETED)


async def test_cancel_on_another_instance_drops_a_queued_task(
    tmp_path, stores
):
    hold = asyncio.Event()
    agent = FakeAgent(("hold", hold))
    first = _surface(tmp_path, stores, [agent])
    second = _surface(tmp_path, stores, [])
    running = await _send(
        _client(first), _message("долго"), return_immediately=True
    )
    queued = await _send(
        _client(first),
        _message("ещё", context_id=running.context_id),
        return_immediately=True,
    )
    canceled = await _client(second).cancel_task(
        pb.CancelTaskRequest(id=queued.id)
    )
    assert canceled.status.state == pb.TASK_STATE_CANCELED
    hold.set()
    await _until(_client(first), running.id, pb.TASK_STATE_COMPLETED)
    await asyncio.sleep(0.2)
    assert [call[0] for call in agent.calls] == ["долго"]
    after = await _client(first).get_task(pb.GetTaskRequest(id=queued.id))
    assert after.status.state == pb.TASK_STATE_CANCELED


async def test_canceling_a_running_task_frees_the_dialog_at_once(
    tmp_path, stores
):
    """Отменённый ход не держит очередь до восстановления."""
    agent = FakeAgent(("hold", asyncio.Event()))
    surface = _surface(tmp_path, stores, [agent])
    client = _client(surface)
    running = await _send(client, _message("долго"), return_immediately=True)
    await _until(client, running.id, pb.TASK_STATE_WORKING)
    queued = await _send(
        client,
        _message("ещё", context_id=running.context_id),
        return_immediately=True,
    )
    await client.cancel_task(pb.CancelTaskRequest(id=running.id))
    await _until(client, queued.id, pb.TASK_STATE_COMPLETED)


async def test_server_stop_leaves_the_task_to_recovery(tmp_path, stores):
    """Остановка сервера — прерывание, не отмена: отметка хода остаётся, а
    реестр отпущен — восстановление другого инстанса прерывает задачу
    (FAILED) сразу, не дожидаясь срока аренды."""
    dying = _surface(tmp_path, stores, [FakeAgent(("hold", asyncio.Event()))])
    task = await _send(
        _client(dying), _message("долго"), return_immediately=True
    )
    await _until(_client(dying), task.id, pb.TASK_STATE_WORKING)
    await dying.astop()
    # Фон остановленного инстанса (``_report``) успевает доработать.
    await asyncio.sleep(0.2)
    survivor = _surface(tmp_path, stores, [])
    assert await survivor.executor.arecover_once() == 1
    failed = await _client(survivor).get_task(pb.GetTaskRequest(id=task.id))
    assert failed.status.state == pb.TASK_STATE_FAILED


async def test_queue_keeps_the_order_the_peer_saw(
    tmp_path, stores, monkeypatch
):
    """Задача, о приёме которой пир уже узнал (``SUBMITTED``), в очереди
    диалога раньше следующей: место в очереди — до ответа пиру."""
    agent = FakeAgent()
    surface = _surface(tmp_path, stores, [agent])
    real = surface.executor._cwd_for
    slow = [True]

    async def cwd_for(context):
        if slow and slow.pop():
            await asyncio.sleep(0.2)
        return await real(context)

    monkeypatch.setattr(surface.executor, "_cwd_for", cwd_for)
    client = _client(surface)
    first = await _send(client, _message("первое"), return_immediately=True)
    second = await _send(
        client,
        _message("второе", context_id=first.context_id),
        return_immediately=True,
    )
    await _until(client, second.id, pb.TASK_STATE_COMPLETED)
    assert [call[0] for call in agent.calls] == ["первое", "второе"]


async def test_message_to_a_working_task_is_refused(tmp_path, stores):
    """Ревью, раунд 2 (R7): сообщение в задачу, чей ход идёт (повтор ответа),
    — отказ запросу; второго хода той же задачи после итога нет, артефакт
    один."""
    hold = asyncio.Event()
    agent = FakeAgent(("ask", "какой цвет?"), ("hold", hold))
    surface = _surface(tmp_path, stores, [agent])
    client = _client(surface)
    asked = await _send(client, _message("покрась"))
    answer = _message("синий", task_id=asked.id, context_id=asked.context_id)
    answering = asyncio.create_task(_send(client, answer))
    await _until(client, asked.id, pb.TASK_STATE_WORKING)
    retry = _message("синий", task_id=asked.id, context_id=asked.context_id)
    try:
        with pytest.raises(Exception, match="не ждёт"):
            await asyncio.wait_for(_send(client, retry), 5)
    finally:
        hold.set()
    done = await answering
    await asyncio.sleep(0.2)
    after = await client.get_task(pb.GetTaskRequest(id=asked.id))
    assert done.status.state == pb.TASK_STATE_COMPLETED
    assert after.status.state == pb.TASK_STATE_COMPLETED
    assert len(after.artifacts) == 1
    assert agent.answers == ["синий"]


async def test_answer_retried_on_another_instance_keeps_the_result(
    tmp_path, stores
):
    """Ревью, раунд 2 (R6): ответ ушёл на gc1 (агент ещё создаётся), его
    повтор — на gc2. Повтор не ставит задаче FAILED: ход gc1 доводит её до
    COMPLETED, ответ модель получает один раз."""
    agent = FakeAgent(("ask", "какой цвет?"))
    first = _surface(tmp_path, stores, [agent])
    second = _surface(tmp_path, stores, [])
    asked = await _send(_client(first), _message("покрась"))
    creating, gate = asyncio.Event(), asyncio.Event()

    async def slow_factory(settings, **_kw):
        creating.set()
        await gate.wait()
        agent.settings = settings
        return agent

    first.executor._agents.clear()  # агент создаётся заново (рестарт)
    first.executor._agent_factory = slow_factory
    answer = _message("синий", task_id=asked.id, context_id=asked.context_id)
    answering = asyncio.create_task(_send(_client(first), answer))
    await asyncio.wait_for(creating.wait(), 5)
    retry = _message("синий", task_id=asked.id, context_id=asked.context_id)
    try:
        await _send(_client(second), retry)
    except Exception:  # отказ повтору — тоже правильный исход
        pass
    gate.set()
    done = await answering
    after = await _client(second).get_task(pb.GetTaskRequest(id=asked.id))
    assert done.status.state == pb.TASK_STATE_COMPLETED
    assert after.status.state == pb.TASK_STATE_COMPLETED
    assert agent.answers == ["синий"]


async def test_snapshot_never_rolls_back_a_final_state(stores):
    """Ревью, раунд 2 (R7): итог задачи окончателен — запоздалый снимок
    (WORKING владельца после отмены с другого инстанса) его не откатывает."""
    from gc_core.threads.facade import Threads
    from gc_server.a2a.task_store import ThreadTaskStore

    tasks = ThreadTaskStore(Threads(stores.threads))
    task = pb.Task(
        id="t-final",
        context_id="ctx-final",
        status=pb.TaskStatus(state=pb.TASK_STATE_WORKING),
    )
    await tasks.save(task, None)
    task.status.state = pb.TASK_STATE_CANCELED
    await tasks.save(task, None)
    late = pb.Task()
    late.CopyFrom(task)
    late.status.state = pb.TASK_STATE_WORKING
    await tasks.save(late, None)
    stored = await tasks.get("t-final", None)
    assert stored.status.state == pb.TASK_STATE_CANCELED


async def test_slow_agent_start_is_not_taken_for_dead(tmp_path, stores):
    """Ревью, раунд 2 (R16): агент создаётся дольше срока аренды реестра
    (медленный MCP) — реестр продлевается, восстановление другого инстанса
    задачу не прерывает, она доходит до COMPLETED."""
    agent = FakeAgent()
    first = _surface(tmp_path, stores, [])
    second = _surface(tmp_path, stores, [])
    _fast_owners(first, second)
    creating, gate = asyncio.Event(), asyncio.Event()

    async def slow_factory(settings, **_kw):
        creating.set()
        await gate.wait()
        agent.settings = settings
        return agent

    first.executor._agent_factory = slow_factory
    client = _client(first)
    task = await _send(client, _message("сделай"), return_immediately=True)
    await asyncio.wait_for(creating.wait(), 5)
    await asyncio.sleep(0.8)
    assert await second.executor.arecover_once() == 0
    gate.set()
    done = await _until(client, task.id, pb.TASK_STATE_COMPLETED)
    assert done.status.state == pb.TASK_STATE_COMPLETED


class _GatedThreads:
    """Обёртка ``Threads``: ``update_frontend``, чей патч подходит под
    ``match``, сперва ждёт ``gate`` — один раз."""

    def __init__(self, threads, match, gate):
        self._threads = threads
        self._match = match
        self._gate = gate

    def __getattr__(self, name):
        return getattr(self._threads, name)

    async def update_frontend(self, key, name, patch, **kwargs):
        if self._gate is not None and self._match(patch):
            gate, self._gate = self._gate, None
            await gate()
        return await self._threads.update_frontend(key, name, patch, **kwargs)


async def test_late_index_does_not_hide_the_final_snapshot(stores):
    """Ревью, раунд 4: индекс снимка и его запись — разные обращения.
    Отмена с другого инстанса легла между записью снимка владельца и его
    индексом — индекс указал на нефинальный снимок, и задача навсегда
    осталась ``INPUT_REQUIRED``, хотя её отменили."""
    from gc_core.threads.facade import Threads
    from gc_server.a2a.task_store import ThreadTaskStore

    release, blocked = asyncio.Event(), asyncio.Event()

    async def gate():
        blocked.set()
        await release.wait()

    def index_only(patch):
        return any(k.startswith("task:") for k in patch) and not any(
            k.startswith("f:") for k in patch
        )

    plain = ThreadTaskStore(Threads(stores.threads))
    working = pb.Task(
        id="t4",
        context_id="c4",
        status=pb.TaskStatus(state=pb.TASK_STATE_WORKING),
    )
    await plain.save(working, None)
    owner = ThreadTaskStore(
        _GatedThreads(Threads(stores.threads), index_only, gate)
    )
    asked = pb.Task()
    asked.CopyFrom(working)
    asked.status.state = pb.TASK_STATE_INPUT_REQUIRED
    late = asyncio.create_task(owner.save(asked, None))
    await blocked.wait()  # снимок владельца записан, индекс — ещё нет
    canceled = pb.Task()
    canceled.CopyFrom(working)
    canceled.status.state = pb.TASK_STATE_CANCELED
    await plain.save(canceled, None)
    release.set()
    await late
    stored = await plain.get("t4", None)
    assert stored.status.state == pb.TASK_STATE_CANCELED


def _gate_both(surfaces, match):
    """Оба инстанса доходят до записи, подходящей под ``match``, и пишут
    её одновременно (тот же прочитанный снимок диалога)."""
    barrier = asyncio.Barrier(len(surfaces))
    for surface in surfaces:
        executor = surface.executor
        executor._threads = _GatedThreads(
            executor._threads, match, barrier.wait
        )


async def test_answer_racing_on_two_instances_runs_once(tmp_path, stores):
    """Ревью, раунд 4: два ответа на вопрос задачи одновременно на двух
    инстансах — оба занимали диалог и запускали ход; проигравший ставил
    общей задаче FAILED. Теперь ход с ответом — один, итог — COMPLETED."""
    hold = asyncio.Event()
    asking = FakeAgent(("ask", "какой цвет?"), ("hold", hold))
    other = FakeAgent()
    first = _surface(tmp_path, stores, [asking])
    second = _surface(tmp_path, stores, [other])
    asked = await _send(_client(first), _message("покрась"))
    assert asked.status.state == pb.TASK_STATE_INPUT_REQUIRED
    _gate_both(
        (first, second),
        lambda patch: "waiting" in patch and bool(patch.get("running")),
    )

    def answer():
        return _message("синий", task_id=asked.id, context_id=asked.context_id)

    sends = [
        asyncio.create_task(_send(_client(surface), answer()))
        for surface in (first, second)
    ]
    await asyncio.sleep(0.3)
    hold.set()
    await asyncio.wait_for(asyncio.gather(*sends, return_exceptions=True), 10)
    stored = await _until(_client(first), asked.id, pb.TASK_STATE_COMPLETED)
    assert stored.status.state == pb.TASK_STATE_COMPLETED
    assert asking.answers + other.answers == ["синий"]


async def test_two_tasks_never_run_in_one_dialog_at_once(tmp_path, stores):
    """Ревью, раунд 4: голова очереди разошлась у двух инстансов (запись
    очереди с ранним временем легла после чужого чтения) — оба сочли
    диалог своим и запустили два хода в одном треде. Теперь второй ждёт."""
    import types

    from gc_core.threads.models import SideChange
    from gc_server.a2a.task_store import FRONTEND

    running = {"now": 0, "max": 0}

    class Counted(FakeAgent):
        """Считает ходы, идущие в диалоге одновременно."""

        async def stream(self, prompt, **kwargs):
            running["now"] += 1
            running["max"] = max(running["max"], running["now"])
            try:
                async for item in super().stream(prompt, **kwargs):
                    yield item
            finally:
                running["now"] -= 1

    hold = asyncio.Event()
    holding = Counted(("hold", hold))
    later = Counted()
    first = _surface(tmp_path, stores, [holding])
    second = _surface(tmp_path, stores, [later])
    second_claiming = asyncio.Event()
    barrier = asyncio.Barrier(2)

    def queue_claim(patch):
        return bool(patch.get("running")) and any(
            k.startswith("q:") and v is None for k, v in patch.items()
        )

    async def gate_first():
        await barrier.wait()

    async def gate_second():
        second_claiming.set()
        await barrier.wait()

    for surface, gate in ((first, gate_first), (second, gate_second)):
        executor = surface.executor
        executor._threads = _GatedThreads(executor._threads, queue_claim, gate)

    async def enqueue_early(self, context_id, task_id, cwd):
        # Время приёма раньше чужого, а запись — после чужого чтения.
        entry = {"at": "2000-01-01T00:00:00+00:00", "instance": "gc1"}
        self._waiting[task_id] = entry
        await second_claiming.wait()
        await self._threads.store.append_side(
            dialog_key(context_id),
            SideChange(
                op_id=f"q_{task_id}",
                patch_frontend=(
                    FRONTEND,
                    {"cwd": cwd, "q:" + task_id: {**entry, "seen": "x"}},
                ),
            ),
        )

    first.executor._aenqueue = types.MethodType(enqueue_early, first.executor)
    context = "ctx-two"
    one = asyncio.create_task(
        _send(_client(first), _message("один", context_id=context))
    )
    await asyncio.sleep(0.05)
    two = asyncio.create_task(
        _send(_client(second), _message("два", context_id=context))
    )
    await asyncio.sleep(0.5)
    hold.set()
    done = await asyncio.wait_for(asyncio.gather(one, two), 10)
    assert running["max"] == 1
    assert len(holding.calls) == len(later.calls) == 1
    assert [task.status.state for task in done] == [
        pb.TASK_STATE_COMPLETED,
        pb.TASK_STATE_COMPLETED,
    ]


async def _registry_entries(stores, executor) -> dict:
    from gc_server import owners

    key = executor._owners.key
    if key is None:
        return {}
    record = await stores.threads.get_thread(owners.registry_key(key))
    return owners.entries(record, "a2a")


async def _until_gone(stores, executor, entry: str) -> None:
    for _ in range(200):
        if entry not in await _registry_entries(stores, executor):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{entry} всё ещё в реестре")


async def test_task_is_in_the_owner_registry_while_it_runs(tmp_path, stores):
    """Находка №13: задачу «в работе» видно в реестре владельца, отметка
    хода в диалоге называет его — восстановление читает реестры, а не все
    диалоги."""
    hold = asyncio.Event()
    surface = _surface(tmp_path, stores, [FakeAgent(("hold", hold))])
    client = _client(surface)
    task = await _send(client, _message("долго"), return_immediately=True)
    await _until(client, task.id, pb.TASK_STATE_WORKING)
    entry = f"{task.context_id}|{task.id}"
    assert entry in await _registry_entries(stores, surface.executor)
    record = await stores.threads.get_thread(dialog_key(task.context_id))
    running = record.frontend["a2a"]["running"]
    assert running["owner"] == surface.executor._owners.key
    assert "at" in running
    hold.set()
    await _until(client, task.id, pb.TASK_STATE_COMPLETED)
    await _until_gone(stores, surface.executor, entry)


async def test_waiting_for_an_answer_is_not_in_the_registry(tmp_path, stores):
    """Задача, ждущая ответа пира, процесса не держит: в реестре её нет, и
    восстановление её не трогает (очередь за вопросом — без срока)."""
    surface = _surface(tmp_path, stores, [FakeAgent(("ask", "какой цвет?"))])
    asked = await _send(_client(surface), _message("покрась"))
    assert asked.status.state == pb.TASK_STATE_INPUT_REQUIRED
    await _until_gone(
        stores, surface.executor, f"{asked.context_id}|{asked.id}"
    )
    record = await stores.threads.get_thread(dialog_key(asked.context_id))
    assert record.frontend["a2a"]["waiting"] == asked.id


async def test_queued_task_is_in_the_registry_with_its_owner(tmp_path, stores):
    hold = asyncio.Event()
    surface = _surface(tmp_path, stores, [FakeAgent(("hold", hold))])
    client = _client(surface)
    first = await _send(client, _message("один"), return_immediately=True)
    await _until(client, first.id, pb.TASK_STATE_WORKING)
    second = await _send(
        client,
        _message("два", context_id=first.context_id),
        return_immediately=True,
    )
    entry = f"{second.context_id}|{second.id}"
    assert entry in await _registry_entries(stores, surface.executor)
    record = await stores.threads.get_thread(dialog_key(first.context_id))
    queued = record.frontend["a2a"]["q:" + second.id]
    assert queued["owner"] == surface.executor._owners.key
    assert "seen" not in queued
    hold.set()
    await _until(client, second.id, pb.TASK_STATE_COMPLETED)
    await _until_gone(stores, surface.executor, entry)


async def test_lost_registry_interrupts_own_turns(tmp_path, stores):
    """Реестр потерян (продление опоздало, его взял восстановитель): свои
    ходы снимаются как прерванные — итог запишет восстановитель."""
    agent = FakeAgent(("hold", asyncio.Event()))
    surface = _surface(tmp_path, stores, [agent])
    client = _client(surface)
    task = await _send(client, _message("долго"), return_immediately=True)
    await _until(client, task.id, pb.TASK_STATE_WORKING)
    await surface.executor._aon_owners_lost()
    assert agent.stopped.is_set()
    await asyncio.sleep(0.1)
    after = await client.get_task(pb.GetTaskRequest(id=task.id))
    assert after.status.state == pb.TASK_STATE_WORKING


class _CountingStore:
    """Хранилище, считающее чтения: восстановление не должно читать диалоги
    живых и давно закрытых задач."""

    READS = ("get_thread", "read_stream", "list_threads")

    def __init__(self, inner) -> None:
        self._inner = inner
        self.reads = 0
        self.listed = 0

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name not in self.READS:
            return attr

        async def counted(*args, **kwargs):
            self.reads += 1
            self.listed += name == "list_threads"
            return await attr(*args, **kwargs)

        return counted


def _count_reads(surface):
    from gc_core.threads.facade import Threads
    from gc_server.a2a.task_store import ThreadTaskStore

    counting = _CountingStore(surface.executor._threads.store)
    threads = Threads(counting)
    surface.executor._threads = threads
    surface.executor._tasks = ThreadTaskStore(threads)
    surface.executor._owners._threads = threads
    return counting


async def _dialogs(stores, first: int, count: int) -> None:
    from gc_core.threads.models import SideChange

    for n in range(first, first + count):
        await stores.threads.append_side(
            dialog_key(f"old-{n}"),
            SideChange(op_id=f"old-{n}", patch_frontend=("a2a", {"cwd": "/"})),
        )


async def _dead_task(tmp_path, stores):
    dying = _surface(tmp_path, stores, [FakeAgent(("hold", asyncio.Event()))])
    _fast_owners(dying)
    task = await _send(
        _client(dying), _message("долго"), return_immediately=True
    )
    await _until(_client(dying), task.id, pb.TASK_STATE_WORKING)
    await _crash(dying)
    return dying, task


async def test_recovery_reads_only_registries_not_dialogs(tmp_path, stores):
    """Находка №13: проход читает каталог, реестры и диалоги задач мёртвых
    — число чтений не растёт с историей диалогов, списка тредов нет."""
    survivor = _surface(tmp_path, stores, [])
    _fast_owners(survivor)
    counting = _count_reads(survivor)
    await _dialogs(stores, 0, 20)
    await _dead_task(tmp_path, stores)
    counting.reads = 0
    assert await survivor.executor.arecover_once() == 1
    few = counting.reads
    await _dialogs(stores, 20, 300)
    await _dead_task(tmp_path, stores)
    counting.reads = 0
    assert await survivor.executor.arecover_once() == 1
    assert counting.reads == few
    assert counting.listed == 0


async def test_one_recoverer_takes_a_dead_registry(tmp_path, stores):
    first = _surface(tmp_path, stores, [])
    second = _surface(tmp_path, stores, [])
    _fast_owners(first, second)
    _dying, task = await _dead_task(tmp_path, stores)
    taken = await asyncio.gather(
        first.executor.arecover_once(), second.executor.arecover_once()
    )
    assert sum(taken) == 1
    failed = await _client(first).get_task(pb.GetTaskRequest(id=task.id))
    assert failed.status.state == pb.TASK_STATE_FAILED


async def test_turn_holding_the_dialog_lease_is_not_interrupted(
    tmp_path, stores
):
    """Аренда треда диалога жива (ход зомби ещё пишет) — задачу не трогают,
    запись реестра ждёт следующего прохода; аренду отпустили — прервана."""
    from datetime import timedelta

    from gc_core.threads.models import LeaseRequest

    survivor = _surface(tmp_path, stores, [])
    _fast_owners(survivor)
    _dying, task = await _dead_task(tmp_path, stores)
    key = dialog_key(task.context_id)
    held = await stores.threads.claim(
        key, LeaseRequest(owner="zombie", ttl=timedelta(minutes=1))
    )
    assert await survivor.executor.arecover_once() == 0
    await stores.threads.release(key, held.lease)
    await asyncio.sleep(0.45)  # аренда восстановителя на реестре истекла
    assert await survivor.executor.arecover_once() == 1


async def test_orphan_entry_is_dropped_without_touching_the_task(
    tmp_path, stores
):
    """Запись реестра без отметки в диалоге (процесс умер между снятием
    отметки и снятием записи) снимается, итог задачи не меняется."""
    from gc_server import owners
    from gc_server.a2a.executor import entry_id

    dying = _surface(tmp_path, stores, [FakeAgent()])
    survivor = _surface(tmp_path, stores, [])
    _fast_owners(dying, survivor)
    done = await _send(_client(dying), _message("сделай"))
    assert done.status.state == pb.TASK_STATE_COMPLETED
    key = await dying.executor._owners.aadd(
        entry_id(done.context_id, done.id),
        {"ctx": done.context_id, "task": done.id},
    )
    await _crash(dying)
    assert await survivor.executor.arecover_once() == 0
    assert await stores.threads.get_thread(owners.registry_key(key)) is None
    after = await _client(survivor).get_task(pb.GetTaskRequest(id=done.id))
    assert after.status.state == pb.TASK_STATE_COMPLETED


async def test_zombie_late_result_does_not_override_failed(tmp_path, stores):
    hold = asyncio.Event()
    dying = _surface(tmp_path, stores, [FakeAgent(("hold", hold))])
    survivor = _surface(tmp_path, stores, [])
    _fast_owners(dying, survivor)
    task = await _send(
        _client(dying), _message("долго"), return_immediately=True
    )
    await _until(_client(dying), task.id, pb.TASK_STATE_WORKING)
    await _crash(dying)
    assert await survivor.executor.arecover_once() == 1
    hold.set()
    await asyncio.sleep(0.3)
    after = await _client(survivor).get_task(pb.GetTaskRequest(id=task.id))
    assert after.status.state == pb.TASK_STATE_FAILED


async def test_waiting_mark_without_the_question_event_is_recovered(
    tmp_path, stores
):
    """Процесс умер между отметкой ожидания и событием пиру: задача так и
    не стала ``INPUT_REQUIRED`` — она ``FAILED``, диалог свободен."""
    from gc_server.a2a.task_store import FRONTEND, RUNNING, WAITING

    dying = _surface(tmp_path, stores, [FakeAgent(("hold", asyncio.Event()))])
    survivor = _surface(tmp_path, stores, [])
    _fast_owners(dying, survivor)
    task = await _send(
        _client(dying), _message("долго"), return_immediately=True
    )
    await _until(_client(dying), task.id, pb.TASK_STATE_WORKING)
    key = dialog_key(task.context_id)
    from gc_core.threads.facade import Threads

    await Threads(stores.threads).update_frontend(
        key, FRONTEND, {RUNNING: None, WAITING: task.id}
    )
    await _crash(dying)
    assert await survivor.executor.arecover_once() == 1
    record = await stores.threads.get_thread(key)
    assert record.frontend["a2a"].get(WAITING) is None
    failed = await _client(survivor).get_task(pb.GetTaskRequest(id=task.id))
    assert failed.status.state == pb.TASK_STATE_FAILED


def test_recovery_runs_every_five_minutes():
    from gc_server.a2a import surface

    assert surface.RECOVERY_INTERVAL_SECONDS == 300.0
