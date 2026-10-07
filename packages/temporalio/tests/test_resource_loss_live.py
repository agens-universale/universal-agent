"""Actual native resource hosts cannot reconnect after process loss."""

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from gc_core.thread_store_file_durable import DurableFileThreadStore
from gc_core_temporalio.client import AgentClient
from gc_core_temporalio.contracts import ActivityCall, RunRequest
from gc_core_temporalio.workflow import AgentWorkflow
from live_support import call, wait_marker, temporal_cli
from restart_interceptor import RestartInterceptor
from temporalio.client import WorkflowFailureError
from temporalio.converter import value_to_type
from temporalio.exceptions import CancelledError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer
from test_workflow_controls import scripted_http  # noqa: F401


@pytest.mark.parametrize("resource", ["mcp", "background"])
async def test_actual_resource_host_sigkill_never_reconnects_or_repeats(
    tmp_path,
    scripted_http,  # noqa: F811
    resource,
):
    def effects(text):
        # Новая сессия после потери процесса допустима; повтор эффекта — нет.
        return [line for line in text.splitlines() if '"open"' not in line]

    binary = temporal_cli()
    url, requests, script = scripted_http
    audit = tmp_path / "effects.jsonl"
    core = {
        "backend": {
            "api_mode": "chat_completions",
            "completions_url": url,
            "chat_completion_kwargs": {"stream": False},
        },
        "agent": {
            "model": "scripted",
            "max_steps": 5,
            "strict_turn_finish_validation": False,
        },
        "tui": {"text_answer_mode": "deny"},
        "logging": {"save_logs": False, "console_trace": False},
    }
    if resource == "mcp":
        core["mcp"] = {
            "servers": {
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
            }
        }
        name, arguments = "mcp_counter_increment", {"delay": 0}
    else:
        name, arguments = (
            "exec_command",
            {
                "command": "echo effect >> effects.jsonl",
                "cwd": str(tmp_path),
                "run_in_background": True,
            },
        )
    script.append([call("first", name, arguments)])
    judge = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "verdict": "safe",
                            "reasons": [],
                            "confidence": 1,
                        }
                    ),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
    }
    if resource == "background":
        script.append(judge)
    script.append([call("second", name, arguments)])
    if resource == "background":
        script.append(judge)
    processes, logs = [], []
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary
    ) as env:
        config = {
            "address": env.client.service_client.config.target_host,
            "state_dir": str(tmp_path),
            "thread_stores": {"files": {"path": str(tmp_path / "threads")}},
            "profiles": {
                "test": {
                    "workspace": str(tmp_path),
                    "core": core,
                }
            },
        }
        config_path = tmp_path / "worker.json"
        config_path.write_text(json.dumps(config))
        marker_path = tmp_path / "checkpoint.json"

        def start(checkpoint):
            log = (tmp_path / f"worker-{len(processes)}.log").open("w")
            logs.append(log)
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("worker_process.py")),
                    str(config_path),
                    checkpoint,
                    str(marker_path),
                ],
                cwd=tmp_path,
                stdout=log,
                stderr=log,
            )
            processes.append(process)
            return process

        try:
            first = start("after_tool_activity")
            handle = await AgentClient(env.client).start(
                RunRequest(
                    "test",
                    "files",
                    "test",
                    "lost",
                    ["Execute the two requested effects."],
                    "ua-resource-local",
                    "ua-model",
                ),
                workflow_id="native-resource-lost",
            )
            await wait_marker(marker_path, first, handle)
            async with asyncio.timeout(5):
                while not audit.exists():
                    await asyncio.sleep(0.02)
            before = audit.read_text()
            assert len(before.splitlines()) == (2 if resource == "mcp" else 1)
            marker = json.loads(marker_path.read_text())["value"]
            first.kill()
            first.wait(timeout=10)
            assert len(requests) == (1 if resource == "mcp" else 2)
            start("none")
            # Ход не падает: первый эффект (одна попытка) не повторяется —
            # модель получает «исход неизвестен»; потерю ресурса второй
            # операции модель видит ошибкой инструмента.
            await asyncio.wait_for(handle.result(), 90)
            # Первый эффект не повторяется. Второй — новая операция модели:
            # MCP-владелец этого хода потерян (ошибка инструмента), а фоновый
            # владелец ленивый и создаётся заново в новом процессе.
            expected = effects(before) * (1 if resource == "mcp" else 2)
            assert effects(audit.read_text()) == expected
            del marker
            messages = DurableFileThreadStore(
                tmp_path / "threads"
            ).load_messages("test", "lost")
            assert len({m["id"] for m in messages}) == len(messages)
            outputs = [
                str(m.get("output") or m.get("content"))
                for m in messages
                if m.get("role") == "tool"
                or m.get("type") == "function_call_output"
            ]
            assert any("ToolOutcomeUnknown" in o for o in outputs), outputs
            if resource == "mcp":
                assert any("ResourceOwnerLost" in o for o in outputs), outputs
            history = await handle.fetch_history()
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
            for log in logs:
                log.close()
        await Replayer(
            workflows=[AgentWorkflow],
            interceptors=[RestartInterceptor()],
        ).replay_workflow(history)
        assert effects(audit.read_text()) == expected


async def test_native_cancellation_during_actual_mcp_teardown(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
):
    from gc_core.execution.resource_host import _close_mcp_owner
    from test_workflow_controls import finish, request, runtime

    monkeypatch.chdir(tmp_path)
    url, requests, script = scripted_http
    script.extend(
        [[call("one", "mcp_counter_increment", {"delay": 0})], [finish()]]
    )
    audit = tmp_path / "teardown-audit.jsonl"
    server = {
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
    entered, release, closed = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    tasks = []
    async with runtime(
        tmp_path, url, core_extra={"mcp": {"servers": {"counter": server}}}
    ) as (env, contexts, _):
        host = contexts.resource_host
        original_open = host.open

        async def close():
            tasks.append(asyncio.current_task())
            entered.set()
            await release.wait()
            await _close_mcp_owner()
            closed.set()

        async def open_owner(*args, **kwargs):
            return await original_open(*args, **kwargs, close=close)

        monkeypatch.setattr(host, "open", open_owner)
        handle = await env.client.start_workflow(
            AgentWorkflow.run,
            request(),
            id="cancel-teardown",
            task_queue="native-controls",
        )
        try:
            await asyncio.wait_for(entered.wait(), 30)
            (owner,) = host._owners.values()
            assert tasks == [owner._task]
            await handle.cancel()
            # The activity cancellation must reach the waiter while the actor
            # still owns the live MCP scopes. It must not cancel that actor.
            await asyncio.sleep(0.2)
            assert not owner._task.done()
            release.set()
            await asyncio.wait_for(closed.wait(), 10)
            with pytest.raises(WorkflowFailureError) as cancelled:
                await asyncio.wait_for(handle.result(), 30)
            assert isinstance(cancelled.value.cause, CancelledError)
            assert (await handle.describe()).status.name == "CANCELED"
            assert owner._task.done()
            assert not owner.lost
            assert len(tasks) == 1
            assert [
                json.loads(line)["event"]
                for line in audit.read_text().splitlines()
            ] == ["open", "increment"]
            assert len(requests) == 2
            assert not contexts._contexts and not host._owners
            assert (
                contexts.stores["files"]
                .read_snapshot("test", "controls")
                .owner_id
                is None
            )
            history = await handle.fetch_history()
        finally:
            release.set()
    await Replayer(
        workflows=[AgentWorkflow],
    ).replay_workflow(history)
    assert len(requests) == 2
