"""Обратные вызовы о завершении задачи вебхука (спека сервера, 14.4).

После хода инстанс-владелец шлёт событие (``task.completed``,
``task.failed``, ``task.cancelled``, ``task.interrupted``) на ``url`` из
``webhook.callbacks`` или на ``callback_url`` запроса — только из
``allowed_urls``. Подпись — Standard Webhooks, ``webhook-id`` — id события,
одинаковый в повторах.
"""

import asyncio
import json

import httpx
import pytest
from gc_server.webhook.callbacks import CallbackSender, allowed_callback
from gc_server.webhook.signatures import verify
from gc_testkit import scripted_model
from server_harness import LOOPBACK_URL, make_webhook

SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
SETTINGS = {
    "backend.api_mode": "chat_completions",
    "backend.completions_url": "http://scripted.invalid/v1/chat/completions",
    "backend.chat_completion_kwargs": {"stream": False},
    "agent.model": "scripted",
    "agent.max_steps": 3,
    "agent.use_local_subagents": False,
    "agent.strict_turn_finish_validation": False,
    "logging.save_logs": False,
    "logging.console_trace": False,
}


def _receiver(codes):
    """Приёмник: отвечает кодами по очереди, запоминает запросы."""
    got: list = []

    def handle(request: httpx.Request) -> httpx.Response:
        got.append(request)
        return httpx.Response(codes[min(len(got), len(codes)) - 1])

    return got, httpx.AsyncClient(transport=httpx.MockTransport(handle))


def _webhook(tmp_path, monkeypatch, callbacks, codes=(200,)):
    scripted_model.install(monkeypatch)
    surface = make_webhook(
        tmp_path, config={"webhook": {"callbacks": callbacks}}, **SETTINGS
    )
    got, client = _receiver(list(codes))
    pauses: list = []

    async def fake_sleep(seconds):
        pauses.append(seconds)

    surface.callbacks.client = client
    surface.callbacks.sleep = fake_sleep
    return surface, got, pauses


async def _run(surface, body):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=surface.app()),
        base_url=LOOPBACK_URL,
    ) as client:
        resp = await client.post("/v1/tasks", json=body)
        if resp.status_code != 202:
            return resp
        task = resp.json()
        for _ in range(400):
            status = (
                await client.get(f"/v1/tasks/generic/{task['task_id']}")
            ).json()
            if status.get("status") == "completed":
                break
            await asyncio.sleep(0.02)
    await surface.callbacks.adrain()
    await surface.astop()
    return resp


async def test_completed_task_is_reported_and_signed(tmp_path, monkeypatch):
    surface, got, _ = _webhook(
        tmp_path,
        monkeypatch,
        {"url": "https://hooks.example/done", "signing_secret": SECRET},
    )
    await _run(surface, {"task_id": "t1", "message": "сделай"})
    assert len(got) == 1
    request = got[0]
    assert str(request.url) == "https://hooks.example/done"
    event = json.loads(request.content)
    assert event["type"] == "task.completed"
    assert event["data"]["task_id"] == "t1"
    assert event["data"]["status"] == "completed"
    assert request.headers["webhook-id"] == event["id"]
    verify(
        [SECRET],
        request.headers,
        request.content,
        now=int(request.headers["webhook-timestamp"]),
        tolerance=1,
    )


async def test_failed_delivery_is_retried_with_backoff(tmp_path, monkeypatch):
    surface, got, pauses = _webhook(
        tmp_path,
        monkeypatch,
        {"url": "https://hooks.example/done", "max_attempts": 4},
        codes=(500, 500, 500, 500),
    )
    await _run(surface, {"task_id": "t2", "message": "сделай"})
    assert len(got) == 4
    assert pauses == [1, 2, 4]
    assert len({r.headers["webhook-id"] for r in got}) == 1


async def test_callback_url_outside_the_allowed_list_is_400(
    tmp_path, monkeypatch
):
    surface, got, _ = _webhook(
        tmp_path,
        monkeypatch,
        {"allowed_urls": ["https://hooks.example/a2a/"]},
    )
    resp = await _run(
        surface,
        {
            "task_id": "t3",
            "message": "x",
            "callback_url": "https://hooks.example.evil/a2a/x",
        },
    )
    assert resp.status_code == 400
    assert got == []


async def test_allowed_callback_url_gets_the_event(tmp_path, monkeypatch):
    surface, got, _ = _webhook(
        tmp_path,
        monkeypatch,
        {"allowed_urls": ["https://hooks.example/a2a/"]},
    )
    await _run(
        surface,
        {
            "task_id": "t4",
            "message": "x",
            "callback_url": "https://hooks.example/a2a/t4",
        },
    )
    assert [str(r.url) for r in got] == ["https://hooks.example/a2a/t4"]


async def test_events_filter(tmp_path, monkeypatch):
    surface, got, _ = _webhook(
        tmp_path,
        monkeypatch,
        {"url": "https://hooks.example/done", "events": ["task.failed"]},
    )
    await _run(surface, {"task_id": "t5", "message": "x"})
    assert got == []


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://hooks.example/a2a/x", True),
        ("https://hooks.example/a2a", False),
        ("https://hooks.example.evil/a2a/x", False),
        ("http://hooks.example/a2a/x", False),
        ("https://hooks.example:8443/a2a/x", False),
        ("https://user@hooks.example/a2a/x", False),
        ("https://hooks.example/a2a/../admin", False),
        ("https://hooks.example/a2a/%2e%2e/admin", False),
        ("https://hooks.example/a2a/x/%2E%2E/%2E%2E/admin", False),
        ("https://hooks.example/a2a/x?y=1", True),
        ("https://hooks.example\\@evil.example/a2a/x", False),
        ("https://hooks.example%2f@evil.example/a2a/x", False),
        ("https://HOOKS.example/a2a/x", True),
    ],
)
def test_allowed_callback_compares_origin_and_path(url, ok):
    assert allowed_callback(url, ["https://hooks.example/a2a/"]) is ok


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://hooks.example/cb", True),
        ("https://hooks.example/cb/t4", True),
        ("https://hooks.example/cb-admin/purge", False),
        ("https://hooks.example/cbx", False),
    ],
)
def test_allowed_prefix_is_whole_path_segments(url, ok):
    """Находка ревью: ``/cb`` в allowed_urls — путь ``/cb`` и всё под
    ним, а не любой путь, начинающийся со строки «/cb»."""
    assert allowed_callback(url, ["https://hooks.example/cb"]) is ok


async def test_sender_gives_up_after_max_attempts():
    got, client = _receiver([503])
    pauses: list = []

    async def fake_sleep(seconds):
        pauses.append(seconds)

    from gc_server.config import CallbacksConfig

    sender = CallbackSender(
        CallbacksConfig(max_attempts=6), client=client, sleep=fake_sleep
    )
    assert not await sender.adeliver("https://h.example/x", {"id": "evt_1"})
    assert len(got) == 6
    assert pauses == [1, 2, 4, 8, 16]


async def test_callback_of_a_running_task_cannot_be_redirected(
    tmp_path, monkeypatch
):
    """Сообщение в идущую задачу не меняет её адрес обратного вызова:
    иначе итог чужой задачи ушёл бы на другой адрес."""
    from gc_core.threads.models import ThreadKey
    from gc_server.webhook import status as task_status

    surface, _got, _ = _webhook(
        tmp_path, monkeypatch, {"allowed_urls": ["https://hooks.example/"]}
    )
    key = ThreadKey(system="generic", thread="t6")
    await task_status.aset_status(
        surface.threads(),
        key,
        status="running",
        run_id="other:1",
        callback_url="https://hooks.example/first",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=surface.app()),
        base_url=LOOPBACK_URL,
    ) as client:
        resp = await client.post(
            "/v1/tasks",
            json={
                "task_id": "t6",
                "message": "x",
                "callback_url": "https://hooks.example/second",
            },
        )
    assert resp.status_code == 409
    record = await surface.threads().get(key)
    assert task_status.status_of(record)["callback_url"] == (
        "https://hooks.example/first"
    )


async def test_stored_callback_is_rechecked_before_sending(
    tmp_path, monkeypatch
):
    """Список allowed_urls сменили после приёма запроса: адрес, которого
    в нём больше нет, событие не получает."""
    from gc_core.threads.models import ThreadKey

    surface, got, _ = _webhook(tmp_path, monkeypatch, {"allowed_urls": []})
    surface.notify(
        ThreadKey(system="generic", thread="t7"),
        {"status": "completed", "callback_url": "https://old.example/x"},
    )
    await surface.callbacks.adrain()
    assert got == []


async def test_queued_request_cannot_set_a_callback(tmp_path, monkeypatch):
    """Адрес задаёт только запрос, запускающий прогон: сообщение в идущую
    задачу без адреса не может его назначить — иначе чужой итог ушёл бы на
    его адрес."""
    from gc_core.threads.models import ThreadKey
    from gc_server.webhook import status as task_status

    surface, _got, _ = _webhook(
        tmp_path, monkeypatch, {"allowed_urls": ["https://hooks.example/"]}
    )
    key = ThreadKey(system="generic", thread="t8")
    await task_status.aset_status(
        surface.threads(), key, status="running", run_id="other:1"
    )
    from gc_core.threads.session_writer import SessionWriter

    await SessionWriter(surface.threads().store, key, owner="runner").aclaim()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=surface.app()),
        base_url=LOOPBACK_URL,
    ) as client:
        resp = await client.post(
            "/v1/tasks",
            json={
                "task_id": "t8",
                "message": "x",
                "callback_url": "https://hooks.example/steal",
            },
        )
    assert resp.status_code == 409
    record = await surface.threads().get(key)
    assert "callback_url" not in (task_status.status_of(record) or {}) or (
        task_status.status_of(record)["callback_url"] is None
    )


async def test_new_run_drops_the_previous_runs_callback(tmp_path, monkeypatch):
    """Адрес — свойство прогона: следующий прогон без callback_url не шлёт
    итог на адрес прошлого."""
    surface, got, _ = _webhook(
        tmp_path, monkeypatch, {"allowed_urls": ["https://hooks.example/"]}
    )
    await _run(
        surface,
        {
            "task_id": "t9",
            "message": "x",
            "callback_url": "https://hooks.example/first",
        },
    )
    surface2, got2, _ = _webhook(
        tmp_path, monkeypatch, {"allowed_urls": ["https://hooks.example/"]}
    )
    await _run(surface2, {"task_id": "t9", "message": "ещё"})
    assert [str(r.url) for r in got] == ["https://hooks.example/first"]
    assert got2 == []
