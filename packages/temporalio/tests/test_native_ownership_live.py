"""Владение тредом снимается на всех путях; смена профиля не ломает сессии."""

import asyncio
import shutil

import pytest
import test_native_workflow_controls as controls
from gc_core.execution.contracts import ExecutionFailure
from gc_core.thread_store_file_durable import DurableFileThreadStore
from gc_core.thread_store_journal import durable_owner_id
from gc_ua_temporalio.native_contracts import UserMessage
from gc_ua_temporalio.native_provider import NativeWorkerConfig
from gc_ua_temporalio.native_worker import build_native_runtime, worker_options
from gc_ua_temporalio.native_workflow import NativeAgentWorkflow
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from test_native_workflow_controls import finish, request, runtime, wait_status

# Фикстура соседнего модуля: скриптованный HTTP-провайдер модели.
scripted_http = controls.scripted_http


def owner(tmp_path):
    return durable_owner_id(str(tmp_path / "threads"), "test", "controls")


async def test_keep_alive_close_after_continue_as_new_releases_owner(
    tmp_path, monkeypatch, scripted_http
):
    monkeypatch.chdir(tmp_path)
    url, _, script = scripted_http
    script.append([finish("first")])
    async with runtime(tmp_path, url) as (env, _, _):
        handle = await env.client.start_workflow(
            NativeAgentWorkflow.run,
            request(keep_alive=True),
            id="controls",
            task_queue="native-controls",
        )
        first_run = handle.first_execution_run_id
        await wait_status(handle, lambda s: s["turns"] == 1)
        async with asyncio.timeout(30):
            while (await handle.describe()).run_id == first_run:
                await asyncio.sleep(0.05)
        # Новый run владеет тредом по resume, без хода.
        assert owner(tmp_path) == "controls"
        await handle.signal(NativeAgentWorkflow.close_session)
        result = await handle.result()
        assert result["turns"] == 1
        assert owner(tmp_path) is None


async def test_failed_session_releases_owner(
    tmp_path, monkeypatch, scripted_http
):
    monkeypatch.chdir(tmp_path)
    url, _, _ = scripted_http
    commit = DurableFileThreadStore.commit_operation

    def failing_commit(self, system_id, thread_id, operation):
        if operation.kind == "persist":
            raise ExecutionFailure("injected persist failure")
        return commit(self, system_id, thread_id, operation)

    monkeypatch.setattr(
        DurableFileThreadStore, "commit_operation", failing_commit
    )
    async with runtime(tmp_path, url) as (env, _, _):
        handle = await env.client.start_workflow(
            NativeAgentWorkflow.run,
            request(),
            id="controls",
            task_queue="native-controls",
        )
        with pytest.raises(WorkflowFailureError):
            await handle.result()
    assert owner(tmp_path) is None


def _config(tmp_path, url, model):
    return NativeWorkerConfig.model_validate(
        {
            "workflow_queue": "native-controls",
            "model_queue": "native-controls",
            "resource_queue": "native-controls",
            "receipts_database": str(tmp_path / "receipts.sqlite"),
            "profiles": {
                "test": {
                    "workspace": str(tmp_path),
                    "core": {
                        "backend": {
                            "api_mode": "chat_completions",
                            "completions_url": url,
                            "chat_completion_kwargs": {"stream": False},
                        },
                        "agent": {
                            "model": model,
                            "max_steps": 10,
                            "use_local_subagents": False,
                            "strict_turn_finish_validation": False,
                        },
                        "tui": {"text_answer_mode": "deny"},
                        "logging": {
                            "save_logs": False,
                            "console_trace": False,
                        },
                    },
                }
            },
        }
    )


async def test_profile_change_on_worker_restart_keeps_sessions(
    tmp_path, monkeypatch, scripted_http
):
    """Как pydantic-ai: новый профиль воркера подхватывается живой сессией.

    Тот же workflow ID после завершения тоже стартует: владелец контекста
    хода привязан к цепочке run, квитанции прошлой цепочки не мешают.
    """
    binary = shutil.which("temporal")
    if binary is None:
        pytest.skip("Temporal CLI is required")
    monkeypatch.chdir(tmp_path)
    url, requests, script = scripted_http
    script.extend([[finish("one")], [finish("two")], [finish("three")]])
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary
    ) as env:

        async def serve(model):
            config = _config(tmp_path, url, model)
            contexts, native, threads = build_native_runtime(config)
            (options,) = worker_options(config, native, threads)
            return contexts, Worker(
                env.client,
                **options,
            )

        contexts, worker = await serve("model-a")
        async with worker:
            handle = await env.client.start_workflow(
                NativeAgentWorkflow.run,
                request(keep_alive=True),
                id="controls",
                task_queue="native-controls",
            )
            await wait_status(
                handle, lambda s: s["turns"] == 1 and s["phase"] == "idle"
            )
        await contexts.aclose()
        assert requests[-1]["model"] == "model-a"

        contexts, worker = await serve("model-b")
        try:
            async with worker:
                await handle.execute_update(
                    NativeAgentWorkflow.submit_message,
                    UserMessage("second", "second task"),
                )
                await wait_status(
                    handle, lambda s: s["turns"] == 2 and s["phase"] == "idle"
                )
                assert requests[-1]["model"] == "model-b"
                await handle.signal(NativeAgentWorkflow.close_session)
                await handle.result()

                again = await env.client.start_workflow(
                    NativeAgentWorkflow.run,
                    request(),
                    id="controls",
                    task_queue="native-controls",
                )
                assert (await again.result())["turns"] == 1
        finally:
            await contexts.aclose()
    assert owner(tmp_path) is None


async def test_terminated_owner_is_taken_over_by_new_session(
    tmp_path, monkeypatch, scripted_http
):
    """terminate не оставляет тред запертым навсегда: workflow-владелец не
    RUNNING — следующая сессия с другим ID снимает его и захватывает тред."""
    monkeypatch.chdir(tmp_path)
    url, _, script = scripted_http
    script.extend([[finish("first")], [finish("second")]])
    async with runtime(tmp_path, url) as (env, _, _):
        handle = await env.client.start_workflow(
            NativeAgentWorkflow.run,
            request(keep_alive=True),
            id="controls",
            task_queue="native-controls",
        )
        await wait_status(
            handle, lambda s: s["turns"] == 1 and s["phase"] == "idle"
        )
        await handle.terminate("operator")
        assert owner(tmp_path) == "controls"
        other = await env.client.start_workflow(
            NativeAgentWorkflow.run,
            request(),
            id="controls-other",
            task_queue="native-controls",
        )
        assert (await other.result())["turns"] == 1
    assert owner(tmp_path) is None


async def test_live_owner_is_not_taken_over(
    tmp_path, monkeypatch, scripted_http
):
    monkeypatch.chdir(tmp_path)
    url, _, script = scripted_http
    script.append([finish("first")])
    async with runtime(tmp_path, url) as (env, _, _):
        handle = await env.client.start_workflow(
            NativeAgentWorkflow.run,
            request(keep_alive=True),
            id="controls",
            task_queue="native-controls",
        )
        await wait_status(
            handle, lambda s: s["turns"] == 1 and s["phase"] == "idle"
        )
        other = await env.client.start_workflow(
            NativeAgentWorkflow.run,
            request(),
            id="controls-other",
            task_queue="native-controls",
        )
        with pytest.raises(WorkflowFailureError):
            await other.result()
        assert owner(tmp_path) == "controls"
        await handle.signal(NativeAgentWorkflow.close_session)
        await handle.result()


async def test_unlock_releases_only_a_dead_owner(
    tmp_path, monkeypatch, scripted_http
):
    """unlock для локального TUI/CLI: живого владельца не снимает, снятого
    terminate — снимает."""
    from gc_ua_temporalio.thread_store_activities import release_stale_owner

    monkeypatch.chdir(tmp_path)
    url, _, script = scripted_http
    script.append([finish("first")])
    async with runtime(tmp_path, url) as (env, provider, _):
        store = provider.stores["files"]
        handle = await env.client.start_workflow(
            NativeAgentWorkflow.run,
            request(keep_alive=True),
            id="controls",
            task_queue="native-controls",
        )
        await wait_status(
            handle, lambda s: s["turns"] == 1 and s["phase"] == "idle"
        )
        args = (store, "test", "controls", env.client)
        assert await release_stale_owner(*args) is None
        assert owner(tmp_path) == "controls"
        await handle.terminate("operator")
        assert await release_stale_owner(*args) == "controls"
        assert owner(tmp_path) is None
