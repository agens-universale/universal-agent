"""Целый ход, файлы, история, trace и повтор postprocess в реальном Temporal."""

import asyncio
import json
import sys
from pathlib import Path

import psutil
import pytest
from gc_core.threads.file_store import FileThreadStore
from gc_core_temporalio import AgentActivity, AgentActivityRequest
from live_support import call, temporal_cli, thread_messages, thread_owner
from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker
from test_plugin_live import agent_settings
from test_workflow_controls import finish, scripted_http  # noqa: F401
from turn_activity_workflow_fixture import TurnActivityWorkflow


async def test_whole_turn_retry_postprocess_and_continuation(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
):
    monkeypatch.chdir(tmp_path)
    url, requests, script = scripted_http
    target = tmp_path / "report.md"
    script.extend(
        [
            [
                call(
                    "write",
                    "fs_write_file",
                    {"path": str(target), "content": "artifact"},
                )
            ],
            [finish("first attempt")],
            [finish("retried")],
            [finish("continued")],
        ]
    )
    store = FileThreadStore(str(tmp_path / "threads"))
    attempts = []

    async def postprocess(request, _answer):
        attempts.append(activity.info().attempt)
        assert target.read_text() == "artifact"
        assert await thread_owner(store, "sdk", request.thread_id) is None
        if len(attempts) == 1:
            raise RuntimeError("upload failed: retry the entire turn")
        return {"artifact": target.read_text(), "tag": request.context["tag"]}

    settings = agent_settings(url, tmp_path).with_overrides(
        {
            "agent.trace_header_enabled": True,
            "agent.trace_id_header_name": "X-Trace-id",
        }
    )
    runner = AgentActivity(
        {"shop": settings},
        thread_store=store,
        postprocess=postprocess,
        heartbeat_interval=0.1,
    )
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=temporal_cli()
    ) as env:
        async with Worker(
            env.client,
            task_queue="whole-turn",
            workflows=[TurnActivityWorkflow],
            activities=[runner.run],
            max_concurrent_activities=1,
        ):
            handle = await env.client.start_workflow(
                TurnActivityWorkflow.run,
                AgentActivityRequest(
                    "shop",
                    "create report",
                    "t1",
                    trace_id="trace-1",
                    context={"tag": "one"},
                ),
                id="whole-turn-1",
                task_queue="whole-turn",
            )
            first = await asyncio.wait_for(handle.result(), 60)
            events = [event async for event in handle.fetch_history_events()]
            second = await asyncio.wait_for(
                env.client.execute_workflow(
                    TurnActivityWorkflow.run,
                    AgentActivityRequest(
                        "shop",
                        "continue report",
                        "t1",
                        trace_id="trace-2",
                        context={"tag": "two"},
                    ),
                    id="whole-turn-2",
                    task_queue="whole-turn",
                ),
                60,
            )
    assert attempts == [1, 2, 1]
    assert "retried" in json.dumps(first.answer)
    assert first.output == {"artifact": "artifact", "tag": "one"}
    assert "continued" in json.dumps(second.answer)
    assert len(requests) == 4
    assert [h["x-trace-id"] for h in requests.headers] == ["trace-1"] * 3 + [
        "trace-2"
    ]
    scheduled = [
        e.activity_task_scheduled_event_attributes.activity_type.name
        for e in events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    ]
    assert scheduled == ["universal_agent.run"]
    assert not any(
        e.event_type
        == EventType.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED
        for e in events
    )
    history = str(await thread_messages(store, "sdk", "t1"))
    assert "create report" in history and "continue report" in history
    assert await thread_owner(store, "sdk", "t1") is None


async def test_cancel_during_real_mcp_call_releases_thread_and_process(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
):
    monkeypatch.chdir(tmp_path)
    url, _requests, script = scripted_http
    script.append([call("slow", "mcp_counter_increment", {"delay": 30})])
    audit = tmp_path / "mcp.jsonl"
    settings = agent_settings(url, tmp_path).with_overrides(
        {
            "mcp.servers": {
                "counter": {
                    "transport": "stdio",
                    "persistent_session": True,
                    "command": sys.executable,
                    "args": [
                        str(Path(__file__).with_name("mcp_fixture_server.py")),
                        "stdio",
                        "0",
                        str(audit),
                    ],
                }
            },
        }
    )
    store = FileThreadStore(str(tmp_path / "threads"))
    runner = AgentActivity(
        {"shop": settings}, thread_store=store, heartbeat_interval=0.1
    )
    env = ActivityEnvironment()
    task = asyncio.create_task(
        env.run(
            runner.run, AgentActivityRequest("shop", "increment", "cancelled")
        )
    )
    try:
        async with asyncio.timeout(15):
            while not audit.exists() or '"increment"' not in audit.read_text():
                if task.done():
                    await task
                    pytest.fail("agent ended before MCP invocation")
                await asyncio.sleep(0.05)
        pid = json.loads(audit.read_text().splitlines()[0])["pid"]
        env.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        assert await thread_owner(store, "sdk", "cancelled") is None
        async with asyncio.timeout(5):
            while psutil.pid_exists(pid):
                await asyncio.sleep(0.05)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.fixture
async def stream_mcp(tmp_path):
    import socket

    processes, servers = [], []
    try:
        for index in range(2):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            audit = tmp_path / f"http-mcp-{index}.jsonl"
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(Path(__file__).with_name("mcp_fixture_server.py")),
                "streamable-http",
                str(port),
                str(audit),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            processes.append(process)
            async with asyncio.timeout(15):
                while True:
                    try:
                        _, writer = await asyncio.open_connection(
                            "127.0.0.1", port
                        )
                        writer.close()
                        await writer.wait_closed()
                        break
                    except OSError:
                        await asyncio.sleep(0.05)
            servers.append((f"http://127.0.0.1:{port}/mcp", audit))
        yield servers
    finally:
        for process in processes:
            process.terminate()
            await asyncio.wait_for(process.wait(), 10)


@pytest.mark.parametrize("cancel", [False, True])
async def test_stream_mcp_closed_in_its_opening_task(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
    stream_mcp,
    cancel,
):
    from gc_core.tools.connect_mcp.stream_client import _SESSION_MANAGER

    monkeypatch.chdir(tmp_path)
    url, _requests, script = scripted_http
    _url, audit = stream_mcp[1]
    script.extend(
        [
            [call("first", "mcp_counter_increment", {"delay": 0})],
            [
                call(
                    "one",
                    "mcp_other_increment",
                    {"delay": 30 if cancel else 0},
                )
            ],
            [call("two", "mcp_counter_increment", {"delay": 0})],
            [finish("done")],
        ]
    )
    closed_in_owner, close_errors = [], []

    class ObserveExit:
        def __init__(self, cm):
            self.cm = cm

        async def __aexit__(self, *args):
            try:
                return await self.cm.__aexit__(*args)
            except Exception as exc:
                close_errors.append(exc)
                raise

    close_stack = _SESSION_MANAGER._aclose_stack

    async def observe_close(cms, *, opened_in=None):
        closed_in_owner.append(opened_in is asyncio.current_task())
        await close_stack([ObserveExit(cm) for cm in cms], opened_in=opened_in)

    monkeypatch.setattr(_SESSION_MANAGER, "_aclose_stack", observe_close)
    settings = agent_settings(url, tmp_path).with_overrides(
        {
            "mcp.servers": {
                name: {
                    "transport": "stream_http",
                    "url": server[0],
                    "persistent_session": True,
                }
                for name, server in zip(
                    ("counter", "other"), stream_mcp, strict=True
                )
            },
        }
    )
    store = FileThreadStore(str(tmp_path / "threads"))
    runner = AgentActivity({"a": settings}, thread_store=store)
    env = ActivityEnvironment()
    task = asyncio.create_task(
        env.run(runner.run, AgentActivityRequest("a", "go", "http"))
    )
    try:
        if cancel:
            async with asyncio.timeout(15):
                while (
                    not audit.exists()
                    or '"increment"' not in audit.read_text()
                ):
                    await asyncio.sleep(0.05)
            env.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10)
        else:
            await asyncio.wait_for(task, 30)
            messages = await thread_messages(store, "sdk", "http")
            outputs = [
                json.loads(m["output"])
                for m in messages
                if m.get("type") == "function_call_output"
                and m.get("call_id") in {"first", "two"}
            ]
            counters = [
                json.loads(value["result"]["content"][0]["text"])
                for value in outputs
            ]
            assert [counter["count"] for counter in counters] == [1, 2]
        assert closed_in_owner and all(closed_in_owner)
        assert not close_errors, close_errors
        assert await thread_owner(store, "sdk", "http") is None
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
