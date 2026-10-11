"""Маршрут модели: GC-UA — агент, id из конфига — прямой бэкенд."""

import asyncio
import json
import threading

import httpx
import pytest
from fastapi.testclient import TestClient
from gc_chat.app import create_app as create_joint
from gc_chat.completions.facade import CompletionsFacade
from gc_chat.completions.http_api import create_app as create_completions
from gc_chat.main import main
from gc_chat.responses.facade import ResponsesFacade
from gc_chat.responses.http_api import create_app as create_responses
from gc_chat.responses.index_store import ResponseIndex
from gc_core.settings import LoadedSettings
from gc_testkit.settings import make_settings

# Host-guard пускает только loopback (gc_chat.http_guard).
LOOPBACK = "http://127.0.0.1"

_URL = "http://llm.test/v1/chat/completions"


class _Agent:
    def __init__(self) -> None:
        self.executor = type("E", (), {})()
        self.executor.last_terminal_outcome = None
        self.executor._ctx = type("C", (), {})()

    async def stream(self, prompt, thread_id=None, system=None):
        del prompt, thread_id, system
        yield ("answer", "итог")

    @property
    def last_answer(self):
        return "итог"

    def stop(self) -> None:
        return None

    def send(self, message) -> None:
        raise AssertionError(message)

    async def aclose(self) -> None:
        return None


class _Auth:
    async def aget_token(self, force: bool = False) -> str:
        del force
        return "backend-secret"


class _Hold(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = threading.Event()
        self.release = asyncio.Event()
        self.timed_out = False

    async def __aiter__(self):
        yield b"data: one\n\n"
        try:
            await asyncio.wait_for(self.release.wait(), timeout=2)
        except TimeoutError:
            self.timed_out = True

    async def aclose(self) -> None:
        self.closed.set()
        self.release.set()


def _settings(
    tmp_path, model: str = "backend-model", **extra
) -> LoadedSettings:
    config = {
        "agent.model": model,
        "paths.threads_data_path": str(tmp_path / "threads"),
        "backend.completions_url": _URL,
        "backend.request_timeout_sec": 12,
        "backend.use_ssl": False,
        "backend.proxy": "http://proxy.test:8080",
        "backend.api_mode": "chat_completions",
        "backend.chat_completion_headers": {"X-Corp": "yes"},
        "chat_api.auth_token": "client-secret",
    }
    return make_settings({**config, **extra})


def _completions(
    tmp_path, *, model: str = "backend-model", auth=None, **extra
):
    threads = tmp_path / "threads"
    threads.mkdir(exist_ok=True)
    created: list[LoadedSettings] = []

    async def factory(settings, **kwargs):
        del kwargs
        created.append(settings)
        return _Agent()

    facade = CompletionsFacade(
        settings=_settings(tmp_path, model, **extra),
        agent_factory=factory,
        auth_connector=auth,
    )
    return create_completions(facade), created, threads


def _install_backend(monkeypatch, response: httpx.Response):
    seen: dict = {"calls": []}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["calls"].append(request)
        return response

    def client(*, verify, cert, proxies):
        seen["verify"] = verify
        seen["cert"] = cert
        seen["proxies"] = proxies
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("gc_chat.direct_completions.get_async_client", client)
    return seen


def _bearer(client: TestClient):
    return {"Authorization": "Bearer client-secret"}


def test_models_list_order_and_empty_config(tmp_path):
    app, _created, _threads = _completions(tmp_path)
    client = TestClient(app, base_url=LOOPBACK)
    models = client.get("/v1/models", headers=_bearer(client))
    body = models.json()
    assert [item["id"] for item in body["data"]] == [
        "GC-UA",
        "backend-model",
    ]
    assert body["data"][0] == {
        "id": "GC-UA",
        "object": "model",
        "created": 0,
        "owned_by": "gc-chat",
    }

    empty, _created, _threads = _completions(tmp_path, model="")
    listed = TestClient(empty, base_url=LOOPBACK).get(
        "/v1/models", headers=_bearer(client)
    )
    assert [item["id"] for item in listed.json()["data"]] == ["GC-UA"]


def test_joint_and_responses_list_the_same_ids(tmp_path):
    threads = tmp_path / "threads"
    threads.mkdir()
    settings = _settings(tmp_path, **{"chat_api.auth_token": ""})

    async def factory(settings, **kwargs):
        del settings, kwargs
        return _Agent()

    responses = ResponsesFacade(
        settings=settings,
        agent_factory=factory,
        index=ResponseIndex(str(threads)),
    )
    completions = CompletionsFacade(settings=settings, agent_factory=factory)
    joint = TestClient(create_joint(responses, completions), base_url=LOOPBACK)
    alone = TestClient(create_responses(responses), base_url=LOOPBACK)
    assert [item["id"] for item in joint.get("/v1/models").json()["data"]] == [
        "GC-UA",
        "backend-model",
    ]
    assert [item["id"] for item in alone.get("/v1/models").json()["data"]] == [
        "GC-UA",
        "backend-model",
    ]


def test_config_model_gc_ua_exits_before_bind(monkeypatch, capsys):
    def load(path, profile_override=None):
        del path, profile_override
        return make_settings(os_backend="local", **{"agent.model": "GC-UA"})

    def run(coro):
        coro.close()
        raise AssertionError("bind")

    monkeypatch.setattr("gc_chat.main.load_settings", load)
    monkeypatch.setattr("gc_chat.main.asyncio.run", run)
    assert main(["--config", "c.yml"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "GC-UA" in captured.err


def test_spaced_and_lower_model_is_not_the_agent(tmp_path, monkeypatch):
    seen = _install_backend(monkeypatch, httpx.Response(200, json={"id": "u"}))
    app, created, _threads = _completions(tmp_path, model=" GC-UA")
    client = TestClient(app, base_url=LOOPBACK)
    listed = [
        item["id"]
        for item in client.get("/v1/models", headers=_bearer(client)).json()[
            "data"
        ]
    ]
    assert listed == ["GC-UA", " GC-UA"]
    direct = client.post(
        "/v1/chat/completions",
        headers=_bearer(client),
        json={
            "model": " GC-UA",
            "messages": [{"role": "user", "content": "a"}],
        },
    )
    assert direct.status_code == 200
    assert created == []
    assert len(seen["calls"]) == 1
    foreign = client.post(
        "/v1/chat/completions",
        headers=_bearer(client),
        json={
            "model": "gc-ua",
            "messages": [{"role": "user", "content": "a"}],
        },
    )
    assert foreign.status_code == 400
    assert foreign.json()["error"]["code"] == "invalid_request"
    assert len(seen["calls"]) == 1


def test_agent_turn_keeps_config_model(tmp_path):
    app, created, threads = _completions(tmp_path)
    client = TestClient(app, base_url=LOOPBACK)
    response = client.post(
        "/v1/chat/completions",
        headers=_bearer(client),
        json={
            "model": "GC-UA",
            "messages": [{"role": "user", "content": "привет"}],
            "temperature": 0.1,
            "tools": [{"type": "function", "function": {"name": "x"}}],
        },
    )
    assert response.status_code == 200
    assert response.json()["model"] == "GC-UA"
    assert response.json()["choices"][0]["message"]["content"] == "итог"
    assert created[0].settings.agent.model == "backend-model"
    assert list(threads.glob("data_*.jsonl")) == []


def test_direct_forwards_body_and_backend_headers(tmp_path, monkeypatch):
    upstream = {
        "id": "cmpl_upstream",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "echo", "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"total_tokens": 3},
    }
    seen = _install_backend(monkeypatch, httpx.Response(200, json=upstream))
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("cert")
    key.write_text("key")
    app, created, threads = _completions(
        tmp_path,
        auth=_Auth(),
        **{"backend.cert_path": str(cert)},
        **{"backend.key_path": str(key)},
    )
    client = TestClient(app, base_url=LOOPBACK)
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "c",
                    "type": "function",
                    "function": {"name": "a", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c", "content": "ok"},
    ]
    body = {
        "model": "backend-model",
        "messages": messages,
        "tools": [{"type": "function", "function": {"name": "echo"}}],
        "temperature": 0.2,
        "logit_bias": {"1": 2},
    }
    response = client.post(
        "/v1/chat/completions",
        headers=_bearer(client),
        json=body,
    )
    assert response.status_code == 200
    assert response.json() == upstream
    assert response.json()["usage"] == {"total_tokens": 3}
    assert created == []
    assert list(threads.glob("data_*.jsonl")) == []
    assert len(seen["calls"]) == 1
    request = seen["calls"][0]
    assert str(request.url) == _URL
    sent = json.loads(request.content)
    assert sent == body
    assert request.headers["authorization"] == "Bearer backend-secret"
    assert request.headers["x-corp"] == "yes"
    assert "client-secret" not in request.headers.get("authorization", "")
    assert seen["verify"] is False
    assert seen["cert"] == (str(cert), str(key))
    assert seen["proxies"] == {
        "http": "http://proxy.test:8080",
        "https": "http://proxy.test:8080",
    }
    assert request.extensions["timeout"]["read"] == 12


def test_direct_stream_is_backend_sse(tmp_path, monkeypatch, capsys):
    raw = b'data: {"id":"cmpl_upstream","choices":[]}\n\n'
    seen = _install_backend(monkeypatch, httpx.Response(200, content=raw))
    app, created, _threads = _completions(tmp_path)
    client = TestClient(app, base_url=LOOPBACK)
    response = client.post(
        "/v1/chat/completions",
        headers=_bearer(client),
        json={
            "model": "backend-model",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.content == raw
    assert b"<think>" not in response.content
    assert b"chatcmpl_" not in response.content
    assert created == []
    assert len(seen["calls"]) == 1
    assert json.loads(seen["calls"][0].content)["stream"] is True
    assert "cmpl_upstream" not in capsys.readouterr().err


def test_foreign_model_does_not_call_backend(tmp_path, monkeypatch):
    seen = _install_backend(monkeypatch, httpx.Response(200, json={"id": "x"}))
    app, created, _threads = _completions(tmp_path)
    client = TestClient(app, base_url=LOOPBACK)
    for model in (None, "", "other", "gc-ua", "GC-UA "):
        payload = {"messages": [{"role": "user", "content": "a"}]}
        if model is not None:
            payload["model"] = model
        response = client.post(
            "/v1/chat/completions",
            headers=_bearer(client),
            json=payload,
        )
        assert response.status_code == 400, model
        assert response.json()["error"]["code"] == "invalid_request"
    assert created == []
    assert seen["calls"] == []


def test_direct_rejects_bad_messages_and_auth(tmp_path, monkeypatch):
    seen = _install_backend(monkeypatch, httpx.Response(200, json={"id": "x"}))
    app, created, _threads = _completions(tmp_path)
    client = TestClient(app, base_url=LOOPBACK)
    denied = client.post(
        "/v1/chat/completions",
        json={
            "model": "backend-model",
            "messages": [{"role": "user", "content": "a"}],
        },
    )
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "invalid_api_key"
    bad = client.post(
        "/v1/chat/completions",
        headers=_bearer(client),
        json={"model": "backend-model", "messages": "nope"},
    )
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "invalid_request"
    assert created == []
    assert seen["calls"] == []


def test_backend_error_and_transport_failure(tmp_path, monkeypatch):
    seen = _install_backend(
        monkeypatch,
        httpx.Response(422, json={"error": {"message": "bad model"}}),
    )
    app, created, _threads = _completions(tmp_path)
    client = TestClient(app, base_url=LOOPBACK)
    body = {
        "model": "backend-model",
        "messages": [{"role": "user", "content": "a"}],
    }
    rejected = client.post(
        "/v1/chat/completions", headers=_bearer(client), json=body
    )
    assert rejected.status_code == 422
    assert rejected.json() == {"error": {"message": "bad model"}}

    seen["calls"].clear()
    _install_backend(monkeypatch, httpx.Response(502, content=b"<html>"))
    html = client.post(
        "/v1/chat/completions", headers=_bearer(client), json=body
    )
    assert html.status_code == 502
    assert html.json()["error"]["code"] == "server_error"
    assert html.json()["error"]["type"] == "invalid_request_error"

    def boom(*, verify, cert, proxies):
        del verify, cert, proxies
        raise httpx.ConnectError("down")

    monkeypatch.setattr("gc_chat.direct_completions.get_async_client", boom)
    failed = client.post(
        "/v1/chat/completions", headers=_bearer(client), json=body
    )
    assert failed.status_code == 500
    assert failed.json()["error"]["code"] == "server_error"
    assert created == []


def test_responses_rejects_other_models_without_a_thread(tmp_path):
    threads = tmp_path / "threads"
    threads.mkdir()
    created: list[LoadedSettings] = []

    async def factory(settings, **kwargs):
        del kwargs
        created.append(settings)
        return _Agent()

    facade = ResponsesFacade(
        settings=make_settings(
            **{"agent.model": "backend-model"},
            **{"paths.threads_data_path": str(threads)},
        ),
        agent_factory=factory,
        index=ResponseIndex(str(threads)),
    )
    client = TestClient(create_responses(facade), base_url=LOOPBACK)
    for model in ("backend-model", "other", "", "gc-ua"):
        response = client.post(
            "/v1/responses", json={"model": model, "input": "привет"}
        )
        assert response.status_code == 400, model
        assert response.json()["error"]["code"] == "invalid_request"
    missing = client.post("/v1/responses", json={"input": "привет"})
    assert missing.status_code == 400
    assert created == []
    index = threads.parent / "responses-index"
    assert not index.exists()
    opened = client.post(
        "/v1/responses", json={"model": "GC-UA", "input": "привет"}
    )
    assert opened.status_code == 200
    assert opened.json()["model"] == "GC-UA"
    assert created[0].settings.agent.model == "backend-model"


async def test_direct_stream_cancel_closes_upstream(tmp_path, monkeypatch):
    hold = _Hold()
    _install_backend(monkeypatch, httpx.Response(200, stream=hold))
    app, created, _threads = _completions(tmp_path)
    raw = json.dumps(
        {
            "model": "backend-model",
            "messages": [{"role": "user", "content": "a"}],
            "stream": True,
        }
    ).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            await asyncio.Event().wait()
        sent = True
        return {"type": "http.request", "body": raw, "more_body": False}

    async def send(message):
        if message["type"] != "http.response.body" or not message.get("body"):
            return None
        assert b"data: one" in message["body"]
        asyncio.current_task().cancel()
        return None

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "headers": [
            (b"host", b"127.0.0.1"),
            (b"content-type", b"application/json"),
            (b"authorization", b"Bearer client-secret"),
        ],
        "client": ("127.0.0.1", 123),
        "server": ("127.0.0.1", 80),
    }
    with pytest.raises(asyncio.CancelledError):
        await app(scope, receive, send)
    assert hold.closed.is_set()
    assert hold.timed_out is False
    assert created == []
