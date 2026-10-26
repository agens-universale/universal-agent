"""trace_id шага (``agent_step(trace_id=…)``) — заголовком в модель, в том
числе из субагента; кривое значение — отказ шага до запуска сессии."""

import asyncio
from dataclasses import replace

import pytest
from gc_core.settings import override_settings
from gc_core.threads.file_store import FileThreadStore
from gc_core_temporalio import AgentPlugin
from gc_core_temporalio.contracts import RunRequest
from gc_core_temporalio.workflow import UniversalAgentWorkflow
from live_support import call, temporal_cli
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from test_plugin_live import agent_settings
from test_workflow_controls import finish, scripted_http  # noqa: F401
from trace_workflow_fixture import TracedWorkflow


def traced_settings(url, tmp_path, *, subagents=False):
    loaded = agent_settings(url, tmp_path)
    core = override_settings(
        loaded.settings,
        {
            "agent.trace_header_enabled": True,
            "agent.trace_id_header_name": "X-Trace-id",
            "agent.use_local_subagents": subagents,
        },
    )
    return replace(loaded, settings=core)


async def run_steps(tmp_path, url, traces, *, subagents=False, start=None):
    plugin = AgentPlugin(
        {"shop": traced_settings(url, tmp_path, subagents=subagents)},
        thread_store=FileThreadStore(str(tmp_path / "threads")),
    )
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=temporal_cli()
    ) as env:
        async with Worker(
            env.client,
            task_queue="traces",
            workflows=[TracedWorkflow],
            plugins=[plugin],
        ):
            workflow, argument = start or (TracedWorkflow.run, traces)
            handle = await env.client.start_workflow(
                workflow, argument, id="traced-steps", task_queue="traces"
            )
            return await asyncio.wait_for(handle.result(), 90)


def sent_traces(requests):
    return [headers.get("x-trace-id") for headers in requests.headers]


async def test_each_step_sends_its_own_trace_to_model(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
):
    monkeypatch.chdir(tmp_path)
    url, requests, script = scripted_http
    script.extend([[finish("first")], [finish("second")]])
    await run_steps(tmp_path, url, ["trace-A", "trace-B"])
    assert sent_traces(requests) == ["trace-A", "trace-B"]


async def test_step_without_trace_sends_no_header(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
):
    monkeypatch.chdir(tmp_path)
    url, requests, script = scripted_http
    script.extend([[finish("plain")]])
    await run_steps(tmp_path, url, [None])
    assert sent_traces(requests) == [None]


async def test_subagent_of_a_step_sends_parent_trace(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
):
    monkeypatch.chdir(tmp_path)
    url, requests, script = scripted_http
    script.extend(
        [
            [
                call(
                    "child",
                    "subagent_worker",
                    {"task": "проверь заказ", "reason": "делегирую"},
                )
            ],
            [finish("child done")],
            [finish("parent done")],
        ]
    )
    await run_steps(tmp_path, url, ["trace-A"], subagents=True)
    child = [
        headers
        for body, headers in zip(requests, requests.headers)
        if any(
            "name: subagent_worker" in str(m.get("content"))
            for m in body["messages"]
            if m.get("role") == "system"
        )
    ]
    assert child, "субагент не дошёл до модели"
    assert set(sent_traces(requests)) == {"trace-A"}


@pytest.mark.parametrize("bad", ["bad\nid", "трейс"])
async def test_invalid_trace_fails_the_step_before_the_model(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
    bad,
):
    monkeypatch.chdir(tmp_path)
    url, requests, _script = scripted_http
    with pytest.raises(WorkflowFailureError) as failure:
        await run_steps(tmp_path, url, [bad])
    cause = failure.value.cause
    assert isinstance(cause, ApplicationError)
    assert cause.type == "InvalidTraceId"
    assert len(requests) == 0


async def test_session_with_invalid_trace_fails_without_retries(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
):
    """RunRequest в обход agent_step: тот же отказ без вечных повторов."""
    monkeypatch.chdir(tmp_path)
    url, requests, _script = scripted_http
    request = RunRequest(
        "shop", "default", "sdk", "direct", ["привет"], trace_id="bad\nid"
    )
    with pytest.raises(WorkflowFailureError) as failure:
        await run_steps(
            tmp_path, url, [], start=(UniversalAgentWorkflow.run, request)
        )
    cause = failure.value.cause
    assert isinstance(cause, ApplicationError)
    assert "trace_id" in str(cause)
    assert len(requests) == 0
