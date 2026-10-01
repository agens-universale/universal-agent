"""HTTP: модели, авторизация, SSE, harbor до bind."""

from fastapi.testclient import TestClient
from gc_chat.main import main
from gc_chat.responses.facade import ResponsesFacade
from gc_chat.responses.http_api import create_app
from gc_chat.responses.index_store import ResponseIndex


class _Agent:
    def __init__(self) -> None:
        self.executor = type("E", (), {})()
        self.executor.last_terminal_outcome = None
        self.executor._ctx = type("C", (), {})()
        self.calls: list[dict] = []

    async def stream(self, prompt, thread_id=None, system=None):
        self.calls.append({"prompt": prompt, "system": system})
        yield ("answer", "ответ")

    @property
    def last_answer(self):
        return "ответ"

    def stop(self) -> None:
        return None

    def send(self, message) -> None:
        raise AssertionError(message)

    async def aclose(self) -> None:
        return None


def _app(tmp_path, *, token: str = "", origins: list[str] | None = None):
    threads = tmp_path / "threads"
    threads.mkdir(exist_ok=True)
    agent = _Agent()

    async def factory(config, **kwargs):
        del config, kwargs
        return agent

    facade = ResponsesFacade(
        config={
            "model": "agent-model",
            "chat_api_auth_token": token,
            "chat_api_cors_origins": list(origins or []),
        },
        agent_factory=factory,
        index=ResponseIndex(str(threads)),
    )
    return create_app(facade), agent


def _events(body: str) -> list[dict]:
    import json

    found = []
    for block in body.split("\n\n"):
        if "data: " not in block:
            continue
        data = block.split("data: ", 1)[1].strip()
        found.append(json.loads(data))
    return found


def test_stream_emits_created_delta_and_completed(tmp_path):
    app, _agent = _app(tmp_path)
    client = TestClient(app)
    response = client.post(
        "/v1/responses",
        json={"model": "GC-UA", "input": "привет", "stream": True},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _events(response.text)
    kinds = [event["type"] for event in events]
    assert kinds[0] == "response.created"
    assert "response.output_text.delta" in kinds
    assert kinds[-1] == "response.completed"
    assert events[-1]["response"]["status"] == "completed"
    assert events[-1]["response"]["model"] == "GC-UA"


def test_models_and_auth_and_cors(tmp_path):
    open_app, _agent = _app(tmp_path)
    client = TestClient(open_app)
    models = client.get("/v1/models")
    assert models.status_code == 200
    ids = [item["id"] for item in models.json()["data"]]
    assert ids == ["GC-UA", "agent-model"]
    assert models.json()["data"][1]["object"] == "model"
    assert models.json()["data"][1]["created"] == 0
    assert "access-control-allow-origin" not in models.headers
    preflight = client.options(
        "/v1/models",
        headers={
            "Origin": "http://anywhere",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "*"

    locked, _agent = _app(tmp_path, token="secret", origins=["http://ui"])
    guarded = TestClient(locked)
    denied = guarded.get("/v1/models")
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "invalid_api_key"
    ok = guarded.get(
        "/v1/models",
        headers={
            "Authorization": "Bearer secret",
            "Origin": "http://ui",
        },
    )
    assert ok.status_code == 200
    assert ok.headers["access-control-allow-origin"] == "http://ui"
    foreign = guarded.options(
        "/v1/models",
        headers={
            "Origin": "http://other",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert foreign.status_code == 400


def test_non_stream_failure_is_500(tmp_path):
    threads = tmp_path / "threads"
    threads.mkdir()

    class Boom(_Agent):
        async def stream(self, prompt, thread_id=None, system=None):
            raise RuntimeError("boom")
            yield ("answer", "")

    async def factory(config, **kwargs):
        del config, kwargs
        return Boom()

    facade = ResponsesFacade(
        config={"model": "m"},
        agent_factory=factory,
        index=ResponseIndex(str(threads)),
    )
    client = TestClient(create_app(facade))
    response = client.post(
        "/v1/responses", json={"model": "GC-UA", "input": "x"}
    )
    assert response.status_code == 500
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "server_error"
    assert body["error"]["message"] == "boom"


def test_harbor_exits_before_bind(monkeypatch, capsys):
    def load(path, profile_override=None):
        del path, profile_override
        return {"os_backend": "harbor"}, None

    def run(coro):
        coro.close()
        raise AssertionError("bind")

    monkeypatch.setattr("gc_chat.main.load_config_ex", load)
    monkeypatch.setattr("gc_chat.main.asyncio.run", run)
    assert main(["--config", "c.yml"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "harbor" in captured.err


def test_local_bind_uses_loopback_and_warns(monkeypatch, capsys, tmp_path):
    captured = {}

    def load(path, profile_override=None):
        del path, profile_override
        return {
            "os_backend": "local",
            "threads_data_path": str(tmp_path),
            "model": "m",
            "strict_turn_finish_validation": True,
            "todo_params": {"reminder_steps": 9},
        }, None

    class FakeServer:
        def __init__(self, config) -> None:
            captured["host"] = config.host
            captured["port"] = config.port
            captured["app"] = config.app

        async def serve(self) -> None:
            return None

    monkeypatch.setattr("gc_chat.main.load_config_ex", load)
    monkeypatch.setattr("gc_chat.main.uvicorn.Server", FakeServer)
    assert main(["--config", "c.yml"]) == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8090
    paths = {getattr(route, "path", None) for route in captured["app"].routes}
    assert "/v1/responses" in paths
    assert "/v1/chat/completions" in paths
    assert "/v1/models" in paths
    preflight = TestClient(captured["app"]).options(
        "/v1/models",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Private-Network": "true",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "*"
    assert preflight.headers["access-control-allow-private-network"] == "true"
    out = capsys.readouterr()
    assert out.out == ""
    assert "без авторизации" in out.err
    assert "event:" not in out.err
    origin = "http://127.0.0.1:8090"
    for line in (
        "GET " + origin + "/",
        "GET " + origin + "/v1/models",
        "POST " + origin + "/v1/responses",
        "GET " + origin + "/v1/responses/{response_id}",
        "POST " + origin + "/v1/responses/{response_id}/cancel",
        "POST " + origin + "/v1/chat/completions",
        "GET " + origin + "/v1/dialogs",
        "GET " + origin + "/v1/dialogs/{thread_id}",
        "GET " + origin + "/v1/dialogs/{thread_id}/artifacts",
        "GET " + origin + "/v1/dialogs/{thread_id}/artifacts/download",
        "GET " + origin + "/v1/workspace/artifacts",
        "GET " + origin + "/v1/workspace/artifacts/download",
    ):
        assert "gc-chat: " + line in out.err
