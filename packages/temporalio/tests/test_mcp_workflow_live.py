"""The ordinary MCP wrapper, in actual native workflow activities and replay."""

import asyncio
import json
import socket
import sys
from pathlib import Path

import pytest
from gc_core_temporalio.workflow import AgentWorkflow
from temporalio.worker import Replayer
from live_support import call
from test_workflow_controls import (  # noqa: F401
    finish,
    request,
    runtime,
    scripted_http,
)


@pytest.mark.parametrize("transport", ["stdio", "stream_sse", "stream_http"])
async def test_mcp_two_calls_share_session_and_replay_does_not_reconnect(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
    transport,
):
    monkeypatch.chdir(tmp_path)
    url, requests, script = scripted_http
    script.extend(
        [
            [call("one", "mcp_counter_increment", {"delay": 0})],
            [call("two", "mcp_counter_increment", {"delay": 0})],
            [finish()],
        ]
    )
    fixture = Path(__file__).with_name("mcp_fixture_server.py")
    process = None
    if transport == "stdio":
        server = {
            "transport": "stdio",
            "persistent_session": True,
            "command": sys.executable,
            "args": [str(fixture), "stdio", "0"],
        }
    else:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        mode = "sse" if transport == "stream_sse" else "streamable-http"
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(fixture),
            mode,
            str(port),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
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
        server = {
            "transport": transport,
            "persistent_session": True,
            "url": f"http://127.0.0.1:{port}",
        }
    try:
        async with runtime(
            tmp_path, url, core_extra={"mcp": {"servers": {"counter": server}}}
        ) as (env, contexts, _):
            handle = await env.client.start_workflow(
                AgentWorkflow.run,
                request(),
                id="mcp",
                task_queue="native-controls",
            )
            result = await asyncio.wait_for(handle.result(), 45)
            assert "done" in str(result["answer"]) and len(requests) == 3
            messages = contexts.stores["files"].load_messages(
                "test", "controls"
            )
            results = [
                json.loads(m["output"])
                for m in messages
                if m.get("type") == "function_call_output"
                and m.get("call_id") in {"one", "two"}
            ]
            assert len(results) == 2
            counters = [
                json.loads(value["result"]["content"][0]["text"])
                for value in results
            ]
            assert [counter["count"] for counter in counters] == [1, 2]
            assert counters[0]["pid"] == counters[1]["pid"]
            assert counters[0]["session"] == counters[1]["session"]
            history = await handle.fetch_history()
            failures = [
                event
                for event in history.events
                if event.HasField("activity_task_failed_event_attributes")
            ]
            assert not failures
            await Replayer(workflows=[AgentWorkflow]).replay_workflow(history)
            assert len(requests) == 3
    finally:
        if process is not None:
            process.terminate()
            await asyncio.wait_for(process.wait(), 10)


async def test_native_child_uses_parent_mcp_actor_and_replays(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
):
    from gc_core_temporalio.child_workflow import SubagentWorkflow

    monkeypatch.chdir(tmp_path)
    url, requests, script = scripted_http
    script.extend(
        [
            [
                call(
                    "delegate",
                    "subagent_worker",
                    {
                        "task": "Increment twice",
                        "reason": "test persistent MCP",
                    },
                )
            ],
            [call("one", "mcp_counter_increment", {"delay": 0})],
            [call("two", "mcp_counter_increment", {"delay": 0})],
            [finish("child done")],
            [finish("parent done")],
        ]
    )
    fixture = Path(__file__).with_name("mcp_fixture_server.py")
    core = {
        "agent": {
            "model": "scripted",
            "max_steps": 10,
            "use_local_subagents": True,
            "strict_turn_finish_validation": False,
        },
        "mcp": {
            "servers": {
                "counter": {
                    "transport": "stdio",
                    "persistent_session": True,
                    "command": sys.executable,
                    "args": [str(fixture), "stdio", "0"],
                }
            }
        },
    }
    async with runtime(tmp_path, url, core_extra=core) as (env, contexts, _):
        handle = await env.client.start_workflow(
            AgentWorkflow.run,
            request(),
            id="child-mcp",
            task_queue="native-controls",
        )
        result = await asyncio.wait_for(handle.result(), 45)
        assert "parent done" in str(result["answer"]) and len(requests) == 5
        history = await handle.fetch_history()
        child_ids = [
            event.child_workflow_execution_started_event_attributes.workflow_execution.workflow_id
            for event in history.events
            if event.HasField(
                "child_workflow_execution_started_event_attributes"
            )
        ]
        assert len(child_ids) == 1
        child_history = await env.client.get_workflow_handle(
            child_ids[0]
        ).fetch_history()
        outputs = [
            json.loads(m["content"])
            for m in requests[3]["messages"]
            if m.get("role") == "tool"
            and m.get("tool_call_id") in {"one", "two"}
        ]
        counters = [
            json.loads(v["result"]["content"][0]["text"]) for v in outputs
        ]
        assert [v["count"] for v in counters] == [1, 2]
        assert counters[0]["session"] == counters[1]["session"]
        assert counters[0]["pid"] == counters[1]["pid"]
        assert not contexts._contexts and not contexts.resource_host._owners
        for saved in (history, child_history):
            await Replayer(
                workflows=[AgentWorkflow, SubagentWorkflow]
            ).replay_workflow(saved)
        assert len(requests) == 5
