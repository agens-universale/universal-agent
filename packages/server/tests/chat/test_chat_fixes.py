"""Каталог реальных инструментов, Bearer и публичные ошибки обоих API."""

import asyncio
import json
import os
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from gc_core.aio import AsyncAgent
from gc_core.backends.local import LocalFsBackend
from gc_core.execution.contracts import ModelFailure, ModelProviderError
from gc_core.tools.connect_mcp.registry import ToolsCache
from gc_core.tools.exec.execute_command import ExecuteCommand
from gc_core.tools.fs.read_file import ReadFile
from gc_server.chat.app import create_app
from gc_server.chat.completions.facade import CompletionsFacade
from gc_server.chat.cwd import cwd_for_new
from gc_server.chat.errors import ApiError
from gc_server.chat.responses.facade import ResponsesFacade
from gc_server.chat.responses.index_store import ResponseIndex
from gc_server.config import ChatConfig, ServerSection
from gc_testkit.settings import make_settings
from pydantic import ValidationError


@pytest.mark.parametrize("cls", [ResponsesFacade, CompletionsFacade])
async def test_concurrent_agents_use_their_own_cwd(tmp_path, monkeypatch, cls):
    discoveries = []

    async def discover(name, config, **kwargs):
        discoveries.append(name)
        return [], {"status": "connected"}

    monkeypatch.setattr(
        "gc_core.tools.connect_mcp.registry._adiscover_one", discover
    )
    process = tmp_path / "process"
    root = tmp_path / "root"
    process.mkdir()
    root.mkdir()
    monkeypatch.chdir(process)
    settings = make_settings(
        {
            "agent.model": "test",
            "logging.save_logs": False,
            "logging.console_trace": False,
            "paths.threads_data_path": str(tmp_path / "threads"),
            "mcp.servers.review": {"transport": "stdio", "command": "unused"},
        }
    )
    shared = LocalFsBackend()
    facade = cls(
        settings=settings,
        agent_factory=AsyncAgent.create,
        os_backend=shared,
        tools_cache=ToolsCache(os_backend=shared),
        section=ChatConfig(launch_root=str(root)),
    )

    async def one(name):
        work = root / name
        work.mkdir()
        (work / "marker.txt").write_text(name)
        agent = await facade._agent_for(
            str(uuid.uuid4()),
            facade.new_dialog_cwd(str(work))
            if cls is ResponsesFacade
            else cwd_for_new(str(work), str(root)),
        )
        try:
            ctx = agent.executor._ctx
            result = json.loads(
                await ExecuteCommand(ctx=ctx).acall(
                    command="cd" if os.name == "nt" else "pwd"
                )
            )
            assert result["ok"], json.dumps(result, ensure_ascii=False)
            assert result["stdout"]["content"].strip() == str(work)
            read = json.loads(await ReadFile(ctx=ctx).acall(path="marker.txt"))
            assert name in read["content"]
            backend = ctx.os_backend
            await backend.write("written.txt", name)
            await backend.copy("written.txt", "copy.txt")
            assert await backend.read("copy.txt") == name
            assert (work / "written.txt").read_text() == name
            return backend
        finally:
            await agent.aclose()

    a, b = await asyncio.gather(one("a"), one("b"))
    assert a is not b
    assert os.getcwd() == str(process)
    assert not (process / "written.txt").exists()
    assert discoveries == ["review"]


def test_cwd_root_is_fixed_at_server_creation_and_symlink_cannot_escape(
    tmp_path, monkeypatch
):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.chdir(root)
    f = ResponsesFacade(
        settings=make_settings(),
        agent_factory=None,
        index=ResponseIndex.for_threads_dir(str(tmp_path / "threads")),
    )
    monkeypatch.chdir(tmp_path)
    assert f.new_dialog_cwd() == str(root)
    child = root / "child"
    child.mkdir()
    assert f.new_dialog_cwd(str(child)) == str(child)
    link = root / "escape"
    link.symlink_to(tmp_path, target_is_directory=True)
    for path in (tmp_path, link):
        with pytest.raises(ApiError, match="outside launch_root"):
            f.new_dialog_cwd(str(path))


class Agent:
    def __init__(self, error, partial=False):
        self.error = error
        self.partial = partial
        self.executor = SimpleNamespace(
            last_terminal_outcome=None, _ctx=SimpleNamespace()
        )
        self.last_answer = ""

    async def stream(self, prompt, **kwargs):
        if self.partial:
            yield "think", "partial"
            yield "answer", "partial"
        raise self.error
        yield

    async def aclose(self):
        pass


def app_for(tmp_path, error, *, token="", factory_error=False, partial=False):
    async def factory(settings, **kwargs):
        del settings, kwargs
        if factory_error:
            raise error
        return Agent(error, partial)

    settings = make_settings(
        {
            "agent.model": "test",
            "paths.threads_data_path": str(tmp_path / "threads"),
        }
    )
    kwargs = dict(
        settings=settings,
        agent_factory=factory,
        section=ChatConfig(auth_token=token, launch_root=str(tmp_path)),
    )
    responses = ResponsesFacade(
        **kwargs,
        index=ResponseIndex.for_threads_dir(str(tmp_path / "threads")),
    )
    return create_app(responses, CompletionsFacade(**kwargs))


@pytest.mark.parametrize(
    "token", [b"xxxxxx", "ток".encode(), "токен".encode(), b""]
)
def test_malformed_bearer_is_401(tmp_path, token):
    with TestClient(
        app_for(tmp_path, RuntimeError(), token="secret"),
        base_url="http://127.0.0.1",
        raise_server_exceptions=False,
    ) as client:
        response = client.get(
            "/v1/models", headers=[(b"authorization", b"Bearer " + token)]
        )
        assert response.status_code == 401
        assert (
            client.get(
                "/v1/models", headers={"Authorization": "Bearer secret"}
            ).status_code
            == 200
        )


@pytest.mark.parametrize("cls", [ChatConfig, ServerSection])
@pytest.mark.parametrize("token", ["секрет", "contains space", "line\nbreak"])
def test_bad_config_token_fails_at_start(cls, token):
    with pytest.raises(ValidationError, match="ASCII"):
        cls(auth_token=token)


def upstream_error(status):
    response = httpx.Response(
        status,
        request=httpx.Request("POST", "https://private.invalid/model"),
        json={"error": {"message": "secret-key"}},
    )
    return httpx.HTTPStatusError(
        "secret-key https://private.invalid/model",
        request=response.request,
        response=response,
    )


@pytest.mark.parametrize("api", ["responses", "chat/completions"])
@pytest.mark.parametrize("factory_error", [False, True])
@pytest.mark.parametrize(
    "status,http_status,code",
    [
        (401, 502, "upstream_authentication_error"),
        (403, 502, "upstream_permission_denied"),
        (429, 503, "upstream_rate_limited"),
        (503, 503, "upstream_unavailable"),
    ],
)
def test_provider_failures_have_safe_public_contract(
    tmp_path, api, factory_error, status, http_status, code, caplog
):
    body = {
        "model": "GC-UA",
        "input": "hello",
        "messages": [{"role": "user", "content": "hello"}],
    }
    with TestClient(
        app_for(tmp_path, upstream_error(status), factory_error=factory_error),
        base_url="http://127.0.0.1",
    ) as client:
        response = client.post("/v1/" + api, json=body)
        assert response.status_code == http_status
        assert response.json()["error"]["code"] == code
        assert "secret-key" not in response.text
        assert "private.invalid" not in response.text
        if factory_error:
            assert any(
                "open turn failed" in record.message and record.exc_info
                for record in caplog.records
            )
        if api == "responses" and not factory_error:
            saved = client.get("/v1/responses/" + response.json()["id"])
            assert saved.json()["error"] == response.json()["error"]


@pytest.mark.parametrize("api", ["responses", "chat/completions"])
def test_stream_failure_after_headers_is_terminal_and_sanitized(tmp_path, api):
    body = {
        "model": "GC-UA",
        "input": "hello",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
    }
    with TestClient(
        app_for(tmp_path, upstream_error(401), partial=True),
        base_url="http://127.0.0.1",
    ) as client:
        response = client.post("/v1/" + api, json=body)
        assert response.status_code == 200
        assert "upstream_authentication_error" in response.text
        assert "secret-key" not in response.text
        assert "private.invalid" not in response.text
        if api == "responses":
            assert "response.failed" in response.text


@pytest.mark.parametrize(
    "error,expected",
    [
        (httpx.ReadTimeout("secret"), (504, "upstream_timeout")),
        (httpx.ConnectError("secret"), (503, "upstream_unavailable")),
        (
            ModelProviderError(
                ModelFailure(
                    kind="provider",
                    error_type="HTTPStatusError",
                    message="secret",
                    details={"status_code": 401},
                )
            ),
            (502, "upstream_authentication_error"),
        ),
        (RuntimeError("secret"), (500, "server_error")),
    ],
)
def test_classified_and_transport_errors(tmp_path, error, expected):
    with TestClient(
        app_for(tmp_path, error), base_url="http://127.0.0.1"
    ) as client:
        response = client.post(
            "/v1/responses", json={"model": "GC-UA", "input": "hi"}
        )
        assert (
            response.status_code,
            response.json()["error"]["code"],
        ) == expected
        assert "secret" not in response.text


@pytest.mark.parametrize(
    "status,expected",
    [
        ("context_length_exceeded", "upstream_error"),
        ("401", "upstream_authentication_error"),
        ("503", "upstream_unavailable"),
        (None, "upstream_error"),
        ({"code": 500}, "upstream_error"),
        (["500"], "upstream_error"),
    ],
)
def test_stream_error_code_is_not_assumed_to_be_http_status(status, expected):
    from gc_core.utils.chat_completions_stream import BackendStreamError
    from gc_server.chat.model_errors import model_api_error

    error = model_api_error(BackendStreamError("private", status_code=status))
    assert error.code == expected
    assert "private" not in error.message


@pytest.mark.parametrize("api", ["responses", "chat/completions"])
@pytest.mark.parametrize("stream", [False, True])
def test_named_stream_error_finishes_request(tmp_path, api, stream):
    from gc_core.utils.chat_completions_stream import BackendStreamError

    app = app_for(
        tmp_path,
        BackendStreamError("private", status_code="context_length_exceeded"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        body = {
            "model": "GC-UA",
            "input": "hi",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": stream,
        }
        response = client.post("/v1/" + api, json=body)
        assert "upstream_error" in response.text
        if api == "responses":
            for run in list(app.state.responses_facade._live.values()):
                client.portal.call(asyncio.wait_for, run.done.wait(), 2)
            assert not app.state.responses_facade._busy
            assert not app.state.responses_facade._admissions


@pytest.mark.parametrize(
    ("name", "status", "code"),
    [
        ("ContextWindowExceeded", 400, "context_length_exceeded"),
        ("PayloadTooLarge", 413, "request_too_large"),
    ],
)
def test_known_execution_error_has_safe_actionable_message(name, status, code):
    from gc_core.execution import contracts
    from gc_server.chat.model_errors import model_api_error

    error = model_api_error(
        getattr(contracts, name)("private backend url and credentials")
    )
    assert (error.status, error.code) == (status, code)
    assert "private" not in error.message
    assert error.message != "Internal server error."


def test_public_api_error_keeps_its_code():
    from gc_server.chat.model_errors import model_api_error

    error = ApiError(400, "invalid_request", "cwd is outside launch_root")
    assert model_api_error(error) is error


@pytest.mark.parametrize("method", ["get", "post"])
def test_response_storage_failures_are_logged_and_return_json(
    tmp_path, monkeypatch, caplog, method
):
    from gc_server.chat.responses.http_api import create_app as responses_app

    facade = ResponsesFacade(
        settings=make_settings(),
        agent_factory=None,
        index=ResponseIndex.for_threads_dir(str(tmp_path / "threads")),
    )

    async def broken(*args):
        raise OSError("private storage detail")

    monkeypatch.setattr(facade._index, "load", broken)
    with TestClient(
        responses_app(facade), base_url="http://127.0.0.1"
    ) as client:
        path = "/v1/responses/resp_test" + (
            "/cancel" if method == "post" else ""
        )
        response = getattr(client, method)(
            path, headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "server_error"
    assert "private storage detail" not in response.text
    assert any(record.exc_info for record in caplog.records)
