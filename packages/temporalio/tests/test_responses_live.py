"""Actual SSE Responses transport, history cache recovery and native replay."""

import asyncio
import json
import shutil
import threading
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from gc_core.agent_executor import AgentExecutor
from gc_core.settings import LoadedSettings, RuntimeEnv
from gc_core.run_context import acreate_run_context
from gc_core.thread_store_file import FileThreadStore
from gc_core.utils.logging_module import release_run_logger
from gc_core_temporalio.contracts import RunRequest
from gc_core_temporalio.provider import WorkerConfig
from gc_core_temporalio.worker import build_runtime, worker_options
from gc_core_temporalio.workflow import UniversalAgentWorkflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from test_parity_live import snapshot


def response(number, name, args):
    return {
        "id": f"resp-{number}",
        "output": [
            {"id": f"reason-{number}", "type": "reasoning", "summary": []},
            {
                "id": f"tool-{number}",
                "type": "function_call",
                "call_id": f"call-{number}",
                "name": name,
                "arguments": json.dumps(args),
                "status": "completed",
            },
        ],
        "usage": {"input_tokens": 30, "output_tokens": 10, "total_tokens": 40},
    }


def finish(number):
    return response(
        number,
        "turn_finish",
        {
            "solution_type": "full_solution",
            "task": "calculate",
            "task_report": "Total = 1350",
            "handoff": "{}",
        },
    )


@pytest.fixture
def responses_http():
    script, requests = [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            requests.append(
                json.loads(
                    self.rfile.read(int(self.headers["content-length"]))
                )
            )
            if not script:
                self.send_error(500, "Script exhausted")
                return
            item = script.pop(0)
            if item is None:
                body = json.dumps(
                    {
                        "error": {
                            "message": "previous_response_id not found in cache"
                        }
                    }
                ).encode()
                status, content_type = 400, "application/json"
            else:
                events = [
                    {
                        "type": "response.created",
                        "response": {"id": item["id"]},
                    },
                    {"type": "response.completed", "response": item},
                ]
                body = "".join(
                    f"event: {e['type']}\ndata: {json.dumps(e)}\n\n"
                    for e in events
                ).encode()
                status, content_type = 200, "text/event-stream"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            f"http://127.0.0.1:{server.server_port}/v1/responses",
            script,
            requests,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("cache_miss", [False, True])
async def test_responses_two_turns_exact_parity_and_cache_recovery(
    tmp_path, monkeypatch, responses_http, cache_miss
):
    binary = shutil.which("temporal")
    if not binary:
        pytest.skip("Temporal CLI required")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(
        env_vars, "env", env_vars.EnvVars(gc_home=tmp_path / "home")
    )
    url, script, requests = responses_http
    turns = [
        response(1, "exec_calculator", {"expression": "3*450"}),
        finish(2),
    ]
    if cache_miss:
        turns.append(None)
    turns.append(finish(3))
    config = WorkerConfig.model_validate(
        {
            "workflow_queue": "responses",
            # Сверка с локальным ходом: тот же набор инструментов.
            "human_input": True,
            "model_queue": "responses",
            "resource_queue": "responses",
            "state_dir": str(tmp_path),
            "thread_stores": {
                "files": {"path": str(tmp_path / "native_threads")}
            },
            "profiles": {
                "test": {
                    "workspace": str(workspace),
                    "core": {
                        "backend": {
                            "api_mode": "responses",
                            "completions_url": url,
                        },
                        "agent": {
                            "model": "scripted",
                            "max_steps": 10,
                            # Под Temporal project memory нет.
                            "stop_tools": ["project_memory"],
                            "strict_turn_finish_validation": False,
                        },
                        "logging": {
                            "save_logs": False,
                            "console_trace": False,
                        },
                    },
                }
            },
        }
    )
    local_config = LoadedSettings(
        settings=config.profiles["test"].core,
        runtime=RuntimeEnv(launch_root=str(workspace)),
    )
    local_store = FileThreadStore(str(tmp_path / "local_threads"))
    ctx = await acreate_run_context(local_config, thread_store=local_store)
    script.extend(deepcopy(turns))
    try:
        for turn in range(2):
            answer = await AgentExecutor(ctx).arun(
                [f"Task {turn}"],
                system="test",
                thread_id="parity",
                message_ids=[f"input-{turn}"],
            )
        assert not script
        local = snapshot(answer, requests, local_store, workspace)
    finally:
        release_run_logger(ctx.logger)
    requests.clear()
    shutil.rmtree(workspace / ".gc", ignore_errors=True)
    shutil.rmtree(tmp_path / "home", ignore_errors=True)
    script.extend(deepcopy(turns))
    contexts, native, threads = build_runtime(config)
    histories = []
    try:
        async with await WorkflowEnvironment.start_local(
            dev_server_existing_path=binary
        ) as env:
            (options,) = worker_options(config, native, threads)
            async with Worker(
                env.client,
                **options,
            ):
                for turn in range(2):
                    handle = await env.client.start_workflow(
                        UniversalAgentWorkflow.run,
                        RunRequest(
                            "test",
                            "files",
                            "test",
                            "parity",
                            [f"Task {turn}"],
                            "responses",
                            "responses",
                            message_ids=[f"input-{turn}"],
                        ),
                        id=f"responses-{turn}",
                        task_queue="responses",
                    )
                    result = await asyncio.wait_for(handle.result(), 40)
                    histories.append(await handle.fetch_history())
            assert not script
            assert len(requests) == (4 if cache_miss else 3)
            assert all(r["stream"] is True for r in requests)
            assert requests[2]["previous_response_id"] == "resp-2"
            if cache_miss:
                assert not requests[3].get("previous_response_id")
                assert "Task 0" in str(requests[3]["input"])
                assert "Task 1" in str(requests[3]["input"])
            actual = snapshot(
                result["answer"], requests, contexts.stores["files"], workspace
            )
            (tmp_path / "local-trace.json").write_text(
                json.dumps(local, ensure_ascii=False, indent=2)
            )
            (tmp_path / "native-trace.json").write_text(
                json.dumps(actual, ensure_ascii=False, indent=2)
            )
            assert actual == local
            count = len(requests)
            for history in histories:
                await Replayer(
                    workflows=[UniversalAgentWorkflow]
                ).replay_workflow(history)
            assert len(requests) == count
    finally:
        await contexts.aclose()
