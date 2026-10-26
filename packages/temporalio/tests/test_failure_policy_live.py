"""Сбой кода workflow роняет только workflow task; доменный отказ — run."""

import asyncio
import shutil

import pytest
from live_support import temporal_cli, worker_kwargs
import test_workflow_controls as controls
from gc_core.agent_executor import AgentExecutor
from gc_core.execution.contracts import ExecutionFailure, InfrastructureFailure
from gc_core_temporalio.operations import session_failure
from gc_core_temporalio.worker import build_runtime
from gc_core_temporalio.workflow import UniversalAgentWorkflow
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowExecutionStatus
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker
from test_ownership_live import _config
from test_workflow_controls import finish, request

scripted_http = controls.scripted_http


def test_session_failure_separates_domain_from_code_errors():
    activity = ActivityError(
        "failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity="w",
        activity_type="universal_agent.model.request",
        activity_id="1",
        retry_state=None,
    )
    try:
        raise ValueError("bad tool input") from activity
    except ValueError as exc:
        domain = exc
    assert isinstance(session_failure(domain), ApplicationError)
    assert session_failure(InfrastructureFailure("x")).type == (
        "InfrastructureFailure"
    )
    assert session_failure(ExecutionFailure("x")).non_retryable
    # Баг и недетерминизм — не отказ сессии: run ждёт исправленного воркера.
    assert session_failure(AttributeError("bug")) is None
    assert session_failure(RuntimeError("sandbox")) is None


async def test_workflow_bug_keeps_run_until_fixed_worker(
    tmp_path, monkeypatch, scripted_http
):
    binary = temporal_cli()
    monkeypatch.chdir(tmp_path)
    url, _, script = scripted_http
    script.append([finish("done")])
    config = _config(tmp_path, url, "scripted")
    original = AgentExecutor.arun

    async def broken(*_args, **_kwargs):
        raise AttributeError("bug in workflow code")

    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary
    ) as env:

        def worker():
            contexts, native, threads = build_runtime(config)
            options = worker_kwargs(config, native, threads)
            # Без песочницы подмена метода ядра видна коду workflow.
            options["workflow_runner"] = UnsandboxedWorkflowRunner()
            return contexts, Worker(env.client, **options)

        monkeypatch.setattr(AgentExecutor, "arun", broken)
        contexts, broken_worker = worker()
        async with broken_worker:
            handle = await env.client.start_workflow(
                UniversalAgentWorkflow.run,
                request(),
                id="controls",
                task_queue="native-controls",
            )
            async with asyncio.timeout(30):
                while True:
                    events = [
                        e.event_type
                        for e in (await handle.fetch_history()).events
                    ]
                    if EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED in events:
                        break
                    await asyncio.sleep(0.2)
        await contexts.aclose()
        assert (
            await handle.describe()
        ).status == WorkflowExecutionStatus.RUNNING

        monkeypatch.setattr(AgentExecutor, "arun", original)
        contexts, fixed_worker = worker()
        try:
            async with fixed_worker:
                result = await asyncio.wait_for(handle.result(), 60)
        finally:
            await contexts.aclose()
        assert result["turns"] == 1
