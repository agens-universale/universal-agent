"""UserStop cancels the request, including retry/media, without losing its result."""

import asyncio
from types import SimpleNamespace

import pytest
from gc_core import tools
from gc_core.tools.connect_mcp import api, client_port
from gc_core.tools.connect_mcp import stream_client as sc
from gc_core.tools.connect_mcp.os_backend_client import (
    OsBackendStatelessClient,
)
from gc_core.user_stop import UserStop, UserStopInterrupt, bind_user_stop
from gc_core.utils.aretry import retry_with_backoff_async
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData


def stop_gate():
    gate = UserStop()
    gate.prepare_run(asyncio.get_running_loop(), None)
    return gate


@pytest.mark.parametrize("outer", [False, True])
async def test_completed_request_wins_over_late_stop(outer):
    gate = stop_gate()

    async def body():
        gate.request()
        return "done"

    async def request():
        return await client_port._mcp_call_under_stop(
            SimpleNamespace(), "action", body
        )

    with bind_user_stop(gate):
        result = (
            await tools._acall_under_stop(
                "mcp_test_action", "1", request, None
            )
            if outer
            else await request()
        )
    assert result == "done"
    await gate.await_settled()


@pytest.mark.parametrize(
    "stage", ["backoff", "request", "media", "os_backend"]
)
async def test_stop_covers_the_whole_mcp_path(monkeypatch, stage):
    gate, entered = stop_gate(), asyncio.Event()
    calls = []
    client = client_port.AgentRuntimeClient("test", {})

    async def action(*_args, **_kwargs):
        calls.append("action")
        if stage == "media":
            return {"content": []}
        entered.set()
        if stage == "backoff":
            raise ConnectionError("offline")
        await asyncio.Event().wait()

    monkeypatch.setattr(client, "_acall_tool_unstopped", action)
    if stage == "media":
        # Transport completes; only rewriting the response blocks remains.
        async def ready(*_args, **_kwargs):
            return {"content": []}

        monkeypatch.setattr(client, "_acall_tool_unstopped", ready)

        async def media(*_args):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(api, "_arewrite_media_blocks", media)
    if stage == "os_backend":
        client = SimpleNamespace(_aoperation=action)
        client.acall_tool = lambda *a, **kw: (
            OsBackendStatelessClient.acall_tool(client, *a, **kw)
        )
    monkeypatch.setattr(api, "resolve_mcp_client", lambda *a, **kw: client)
    monkeypatch.setattr(
        api,
        "retry_with_backoff_async",
        lambda **kw: retry_with_backoff_async(initial_delay_sec=0.5, **kw),
    )

    async def body():
        return await api.acall_mcp_tool(
            "mcp_test_action",
            {},
            {"test": {"transport": "http", "url": "http://unused"}},
            {
                "mcp_test_action": {
                    "tool": {"parameters": {}},
                    "metadata": {
                        "mcp_server_key": "test",
                        "mcp_tool_name": "action",
                    },
                }
            },
            default_use_retry=True,
        )

    async def work():
        with bind_user_stop(gate):
            return await tools._acall_under_stop(
                "mcp_test_action", "1", body, None
            )

    task = asyncio.create_task(work())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        gate.request()
        done, _ = await asyncio.wait({task}, timeout=0.3)
        assert done, f"stop was ignored during {stage}"
        with pytest.raises(UserStopInterrupt) as caught:
            await task
        assert caught.value.started
        assert len(calls) <= 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancel_during_cached_session_close_finishes_owner(monkeypatch):
    manager = sc._PersistentSessionManager()
    monkeypatch.setattr(sc, "_SESSION_MANAGER", manager)
    entered, closed = asyncio.Event(), []
    key = "stream_http::http://unused"

    class Resource:
        def __init__(self, name, slow=False):
            self.name, self.slow = name, slow

        async def __aexit__(self, *_args):
            closed.append(self.name)
            if self.slow:
                entered.set()
                await asyncio.Event().wait()

    async def install():
        manager.set_session(
            key, object(), (Resource("transport"), Resource("session", True))
        )

    async def failed_call(*_args, **_kwargs):
        raise ConnectionError("lost connection")

    async def unexpected_open(*_args, **_kwargs):
        pytest.fail("cancelled call must not open another session")

    monkeypatch.setattr(sc, "_acall_tool_on_session", failed_call)
    monkeypatch.setattr(sc, "_aopen_persistent_http_session", unexpected_open)
    await manager.call(key, install)
    owner = next(iter(manager._workers.values())).task
    task = asyncio.create_task(
        sc.acall_tool_stream_http(
            "http://unused",
            {"persistent_session": True},
            None,
            1,
            "action",
            {},
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=1)
        assert done, (
            "caller waits for an owner which returned to an empty queue"
        )
        with pytest.raises(asyncio.CancelledError):
            await task
        assert owner.done()
        assert closed == ["session", "transport"]
        assert not manager._workers and not manager._sessions
    finally:
        owner.cancel()
        await asyncio.gather(owner, task, return_exceptions=True)


@pytest.mark.parametrize("failure", ["initialize", "tool", "terminated"])
async def test_http_fallback_depends_on_the_failed_stage(monkeypatch, failure):
    manager = sc._PersistentSessionManager()
    monkeypatch.setattr(sc, "_SESSION_MANAGER", manager)
    initialized, called, endpoints, contexts = [], [], [], []

    class Resource:
        async def __aenter__(self):
            self.owner = asyncio.current_task()
            contexts.append(self)
            self.closed = False
            return self

        async def __aexit__(self, *_args):
            assert self.owner is asyncio.current_task()
            self.closed = True

    class Transport(Resource):
        async def __aenter__(self):
            await super().__aenter__()
            return None, None, None

    class Session(Resource):
        def __init__(self, *_args):
            pass

        async def initialize(self):
            initialized.append(self)
            if len(initialized) == 1 and failure != "tool":
                message = (
                    "Session terminated"
                    if failure == "terminated"
                    else "rejected"
                )
                raise McpError(ErrorData(code=-32602, message=message))

        async def call_tool(self, *_args, **_kwargs):
            called.append(self)
            if failure == "tool":
                raise McpError(ErrorData(code=-32603, message="tool failed"))
            return SimpleNamespace(content=[], isError=False)

    def transport(endpoint, **_kwargs):
        endpoints.append(endpoint)
        return Transport()

    monkeypatch.setattr(
        sc, "_make_stream_http_client", lambda **kw: Resource()
    )
    monkeypatch.setattr(sc, "streamable_http_client", transport)
    monkeypatch.setattr(sc, "ClientSession", Session)
    monkeypatch.setattr(
        sc, "_stream_http_endpoints", lambda url: [url + "/a", url + "/b"]
    )
    try:
        call = sc.acall_tool_stream_http(
            "http://unused",
            {"persistent_session": True},
            None,
            1,
            "action",
            {},
        )
        if failure == "tool":
            with pytest.raises(McpError, match="tool failed"):
                await call
            assert len(initialized) == 1
        else:
            assert await call == {"content": [], "isError": False}
            assert len(initialized) == 2
            assert endpoints == [
                "http://unused/a",
                "http://unused/" + ("b" if failure == "terminated" else "a"),
            ]
        assert len(called) == 1
    finally:
        await manager.aclose_all()
    assert all(cm.closed for cm in contexts)


@pytest.mark.parametrize("error_kind", ["rpc", "started", "not_started"])
async def test_tool_error_or_stop_is_not_repeated_by_outer_retry(
    monkeypatch, error_kind
):
    calls = []
    failure = (
        McpError(ErrorData(code=-32603, message="tool failed"))
        if error_kind == "rpc"
        else UserStopInterrupt(kind="mcp", started=error_kind == "started")
    )

    class Session:
        async def call_tool(self, *_args, **_kwargs):
            calls.append("effect")
            raise failure

    async def request(*_args, **_kwargs):
        return await sc._call_tool_request(Session(), "action", {})

    target = SimpleNamespace(
        client=SimpleNamespace(acall_tool=request),
        server_key="test",
        base_tool_name="action",
        arguments_obj={},
        use_retry=True,
    )
    monkeypatch.setattr(api, "_prepare_mcp_call", lambda *a, **kw: target)
    with pytest.raises(type(failure), match=str(failure)):
        await asyncio.wait_for(
            api.acall_mcp_tool("mcp_test_action", {}, {}, {}), 1
        )
    assert calls == ["effect"]
