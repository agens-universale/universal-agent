"""API ``/v1`` задач (спека сервера, 14.1): вебхук и swarm — одни ручки.

Вебхук ведёт ход в процессе (скриптованная модель, треды — в SQLite), swarm —
в песочнице (запуск подменён).
"""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from gc_server.app import ServerRuntime
from gc_server.config import ServerConfig
from gc_server.stores import aopen_process_stores
from gc_server.swarm import storage as swarm_storage
from gc_server.webhook.surface import WebhookSurface
from gc_testkit import scripted_model
from gc_testkit.settings import make_settings
from server_harness import make_swarm


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
            "thread_store.inbox_poll_seconds": 0.01,
            "storage": {
                "threads": {"type": "sqlite", "path": str(tmp_path / "t.db")}
            },
        }
    )


def _install(monkeypatch, plan=None):
    from gc_core.utils import connect_backend

    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content or b"{}"))
        if plan is not None:
            await plan(len(seen) - 1)
        return httpx.Response(200, json=scripted_model.FINISH)

    monkeypatch.setattr(
        connect_backend,
        "get_async_client",
        lambda **_kw: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    return seen


@pytest.fixture()
async def webhook(tmp_path):
    stores = await aopen_process_stores(_settings(tmp_path))
    surface = WebhookSurface(
        ServerRuntime(
            base=_settings(tmp_path), config=ServerConfig(), stores=stores
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=surface.app()),
        base_url="http://test",
    ) as client:
        client.surface = surface
        yield client
    await surface.astop()
    await stores.aclose()


async def _done(client, system, task_id, status="completed"):
    for _ in range(400):
        body = (await client.get(f"/v1/tasks/{system}/{task_id}")).json()
        if body.get("status") == status:
            return body
        await asyncio.sleep(0.02)
    raise AssertionError(f"не дождались {status}: {body}")


def _user_text(payload) -> str:
    return " ".join(
        str(m.get("content"))
        for m in payload.get("messages", [])
        if m.get("role") == "user"
    )


async def test_message_and_metadata_reach_the_agent(webhook, monkeypatch):
    seen = _install(monkeypatch)
    resp = await webhook.post(
        "/v1/tasks",
        json={
            "task_id": "t1",
            "message": "почини тест",
            "metadata": {"ticket": "BUG-1"},
        },
    )
    assert resp.status_code == 202
    assert resp.json() == {
        "status": "started",
        "system": "generic",
        "task_id": "t1",
        "message_seq": 1,
    }
    done = await _done(webhook, "generic", "t1")
    assert done["pending_messages"] == 0
    text = _user_text(seen[0])
    assert "почини тест" in text and "BUG-1" in text


async def test_without_message_the_whole_body_is_the_request(
    webhook, monkeypatch
):
    seen = _install(monkeypatch)
    await webhook.post("/v1/tasks", json={"task_id": "t2", "x": 1})
    await _done(webhook, "generic", "t2")
    assert '"x": 1' in _user_text(seen[0])


@pytest.mark.parametrize(
    "body",
    [
        [1, 2],
        {"task_id": "t3", "message": 5},
        {"task_id": "t3", "message": "x", "metadata": "нет"},
        {"task_id": "../etc", "message": "x"},
        {"task_id": "t3", "system": "cli", "message": "x"},
    ],
)
async def test_bad_body_is_400_and_writes_nothing(webhook, body):
    resp = await webhook.post("/v1/tasks", json=body)
    assert resp.status_code == 400
    listed = (await webhook.get("/v1/tasks")).json()
    assert listed["tasks"] == []


async def test_list_pages_with_a_cursor(webhook, monkeypatch):
    _install(monkeypatch)
    for task in ("a", "b", "c"):
        await webhook.post("/v1/tasks", json={"task_id": task, "message": "x"})
        await _done(webhook, "generic", task)
    first = (await webhook.get("/v1/tasks", params={"limit": 2})).json()
    assert len(first["tasks"]) == 2 and first["next_cursor"]
    rest = (
        await webhook.get(
            "/v1/tasks", params={"limit": 2, "cursor": first["next_cursor"]}
        )
    ).json()
    found = {t["task_id"] for t in first["tasks"] + rest["tasks"]}
    assert found == {"a", "b", "c"}
    assert all(t["status"] == "completed" for t in first["tasks"])


async def test_history_pages(webhook, monkeypatch):
    _install(monkeypatch)
    await webhook.post("/v1/tasks", json={"task_id": "h", "message": "x"})
    await _done(webhook, "generic", "h")
    first = (
        await webhook.get("/v1/tasks/generic/h/messages", params={"limit": 1})
    ).json()
    assert len(first["messages"]) == 1
    assert first["next_after"] == first["messages"][0]["seq"]
    rest = (
        await webhook.get(
            "/v1/tasks/generic/h/messages",
            params={"after": first["next_after"]},
        )
    ).json()
    assert (
        rest["messages"] and rest["messages"][0]["seq"] > first["next_after"]
    )
    assert (await webhook.get("/v1/tasks/generic/nope")).status_code == 404


async def test_cancel_stops_the_running_task(webhook, monkeypatch):
    hold = asyncio.Event()

    async def plan(index):
        if index == 0:
            await hold.wait()

    _install(monkeypatch, plan)
    await webhook.post("/v1/tasks", json={"task_id": "c", "message": "x"})
    await _done(webhook, "generic", "c", status="running")
    resp = await webhook.post("/v1/tasks/generic/c/cancel")
    assert resp.status_code == 202
    hold.set()
    await _done(webhook, "generic", "c", status="cancelled")


async def test_events_stream_messages_and_status(webhook, monkeypatch):
    _install(monkeypatch)
    await webhook.post("/v1/tasks", json={"task_id": "e", "message": "x"})
    events: list = []
    async with webhook.stream("GET", "/v1/tasks/generic/e/events") as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        async for line in resp.aiter_lines():
            if line.startswith("event: "):
                events.append(line.removeprefix("event: "))
    assert "message" in events
    assert events[-1] == "status"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/events/webhook"),
        ("GET", "/runs/generic/t"),
        ("GET", "/logs/generic/t"),
        ("GET", "/logs/all"),
    ],
)
async def test_old_paths_are_gone(webhook, method, path):
    assert (await webhook.request(method, path)).status_code == 404


def test_swarm_has_the_same_handles(tmp_path, monkeypatch):
    threads, sandbox = tmp_path / "threads", tmp_path / "sandbox"
    threads.mkdir()
    sandbox.mkdir()
    surface = make_swarm(
        {
            "threads_dir": str(threads),
            "sandbox_root_dir": str(sandbox),
            "max_concurrent": 10,
            "ttl_seconds": 86400,
            "limits": {},
            "cli_command": "gc-cli",
            "wrapper_path": "/usr/bin/true",
            "sandbox_tool": "bubblewrap",
            "agentmd_path": "",
            "skills_dir": "",
        },
        tmp_path,
    )
    started: list = []

    def fake_start(
        swarm_cfg, *, system, task_id, payload, trace_id=None, query=None
    ):
        sid = f"swarm_{system}_{task_id}"
        started.append(query)
        swarm_storage.save_swarm_meta(
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
    monkeypatch.setattr("os.killpg", lambda pid, sig: None)
    client = TestClient(surface.app())
    resp = client.post("/v1/tasks", json={"task_id": "s1", "message": "x"})
    assert resp.status_code == 202
    assert resp.json()["status"] == "started"
    assert started == ["x"]
    assert client.get("/v1/tasks/generic/s1").json()["status"] == "running"
    assert [t["task_id"] for t in client.get("/v1/tasks").json()["tasks"]] == [
        "s1"
    ]
    assert client.get("/v1/tasks/generic/s1/messages").status_code == 200
    cancel = client.post("/v1/tasks/generic/s1/cancel")
    assert cancel.status_code == 202, cancel.json()
    assert client.get("/v1/tasks/generic/s1").json()["status"] == "cancelled"
    assert client.post("/events/webhook", json={}).status_code == 404
