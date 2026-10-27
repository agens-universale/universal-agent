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
