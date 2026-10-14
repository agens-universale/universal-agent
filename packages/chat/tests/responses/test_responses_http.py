"""HTTP: модели, авторизация, SSE, harbor до bind."""

from fastapi.testclient import TestClient
from gc_chat.main import main
from gc_chat.responses.facade import ResponsesFacade
from gc_chat.responses.http_api import create_app
from gc_chat.responses.index_store import ResponseIndex
from gc_testkit.settings import make_settings

# Host-guard пускает только loopback (gc_chat.http_guard).
LOOPBACK = "http://127.0.0.1"


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

    async def factory(settings, **kwargs):
        del settings, kwargs
        return agent

    facade = ResponsesFacade(
        settings=make_settings(
            {
                "agent.model": "agent-model",
                "chat_api.auth_token": token,
                "chat_api.cors_origins": list(origins or []),
            }
        ),
        agent_factory=factory,
        index=ResponseIndex.for_threads_dir(str(threads)),
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
    client = TestClient(app, base_url=LOOPBACK)
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
    client = TestClient(open_app, base_url=LOOPBACK)
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
    # Пустой cors_origins — CORS выключен: чужой origin ничего не получает.
    assert "access-control-allow-origin" not in preflight.headers

    locked, _agent = _app(tmp_path, token="secret", origins=["http://ui"])
    guarded = TestClient(locked, base_url=LOOPBACK)
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

    async def factory(settings, **kwargs):
        del settings, kwargs
        return Boom()

    facade = ResponsesFacade(
        settings=make_settings(**{"agent.model": "m"}),
        agent_factory=factory,
        index=ResponseIndex.for_threads_dir(str(threads)),
    )
    client = TestClient(create_app(facade), base_url=LOOPBACK)
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
        return make_settings(os_backend="harbor")

    def run(coro):
        coro.close()
        raise AssertionError("bind")

    monkeypatch.setattr("gc_chat.main.load_settings", load)
    monkeypatch.setattr("gc_chat.main.asyncio.run", run)
    assert main(["--config", "c.yml"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "harbor" in captured.err


def test_local_bind_uses_loopback_and_warns(monkeypatch, capsys, tmp_path):
    captured = {}

    def load(path, profile_override=None):
        del path, profile_override
        return make_settings(
            {
                "os_backend": "local",
                "paths.threads_data_path": str(tmp_path),
                "agent.model": "m",
                "agent.strict_turn_finish_validation": True,
                "todo.reminder_steps": 9,
            }
        )

    class FakeServer:
        def __init__(self, config) -> None:
            captured["host"] = config.host
            captured["port"] = config.port
            captured["app"] = config.app

        async def serve(self) -> None:
            return None

    monkeypatch.setattr("gc_chat.main.load_settings", load)
    monkeypatch.setattr("gc_chat.main.uvicorn.Server", FakeServer)
    assert main(["--config", "c.yml"]) == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8090
    paths = {getattr(route, "path", None) for route in captured["app"].routes}
    assert "/v1/responses" in paths
    assert "/v1/chat/completions" in paths
    assert "/v1/models" in paths
    preflight = TestClient(captured["app"], base_url=LOOPBACK).options(
        "/v1/models",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Private-Network": "true",
        },
    )
    assert "access-control-allow-origin" not in preflight.headers
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


def test_foreign_site_cannot_start_turn(tmp_path):
    """Simple-запрос браузера (text/plain, без preflight) не доходит до агента."""
    app, agent = _app(tmp_path, origins=["http://ui"])
    client = TestClient(app, base_url=LOOPBACK)
    body = '{"model": "GC-UA", "input": "rm -rf ~"}'
    sneaky = client.post(
        "/v1/responses",
        content=body,
        headers={
            "Content-Type": "text/plain",
            "Origin": "https://evil.example",
        },
    )
    assert sneaky.status_code == 415
    assert agent.calls == []
    form = client.post("/v1/chat/completions", data={"x": "1"})
    assert form.status_code == 415


def test_rebound_host_is_rejected(tmp_path):
    """DNS rebinding: чужой домен на 127.0.0.1 приходит со своим Host."""
    app, agent = _app(tmp_path)
    rebound = TestClient(app, base_url="http://evil.example:8090")
    response = rebound.post(
        "/v1/responses", json={"model": "GC-UA", "input": "x"}
    )
    assert response.status_code == 400
    assert agent.calls == []
    local = TestClient(app, base_url="http://localhost:8090")
    assert local.get("/v1/models").status_code == 200


def test_wide_bind_skips_host_check(tmp_path):
    """Bind не на loopback — проверка Host выключена, доступ держит токен."""
    threads = tmp_path / "threads"
    threads.mkdir()

    async def factory(settings, **kwargs):
        return _Agent()

    facade = ResponsesFacade(
        settings=make_settings(
            **{"agent.model": "m", "chat_api.host": "0.0.0.0"}
        ),
        agent_factory=factory,
        index=ResponseIndex.for_threads_dir(str(threads)),
    )
    client = TestClient(create_app(facade), base_url="http://gpu-box:8090")
    assert client.get("/v1/models").status_code == 200
