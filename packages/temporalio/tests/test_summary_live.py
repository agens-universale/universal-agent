"""Real automatic summary algorithms, full HTTP/local/native trace parity."""

import asyncio
import json
import shutil
import threading
from copy import deepcopy
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from gc_core.agent_executor import AgentExecutor
from gc_core.settings import LoadedSettings, RuntimeEnv
from gc_core.run_context import acreate_run_context
from gc_core.threads.file_store import FileThreadStore
from gc_core.utils.logging_module import release_run_logger
from gc_testkit.helpers.summary_sample import valid_summary_xml_for_tests
from gc_core_temporalio.contracts import RunRequest
from live_support import seed_messages, thread_messages
from gc_core_temporalio.provider import WorkerConfig
from gc_core_temporalio.worker import build_runtime
from gc_core_temporalio.workflow import UniversalAgentWorkflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from live_support import call, temporal_cli, worker_kwargs
from test_parity_live import normalize, snapshot
from test_workflow_controls import finish


@pytest.fixture
def summary_http():
    requests, categories = [], []
    scenario = {"name": "pre_run", "main": 0, "summary": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            request = json.loads(
                self.rfile.read(int(self.headers["content-length"]))
            )
            requests.append(request)
            system = str(request["messages"][0]["content"])
            calls, text = None, None
            if system.startswith("You summarize dialogue context"):
                scenario["summary"] += 1
                category = "summary"
                text = (
                    "invalid summary"
                    if scenario["name"] == "repair"
                    and scenario["summary"] == 1
                    else valid_summary_xml_for_tests(seed="VERIFIED=42")
                )
            elif system.startswith("You fix an invalid <summary>"):
                category, text = (
                    "repair",
                    valid_summary_xml_for_tests(seed="VERIFIED=42"),
                )
            elif system.startswith("You reconcile per-thread session memory"):
                category = "memory"
                text = "<goal>Verify facts</goal><constraints></constraints><state></state><knowledge>VERIFIED=42</knowledge>"
            else:
                category = "main"
                scenario["main"] += 1
                calls = (
                    [call("calc", "exec_calculator", {"expression": "6 * 7"})]
                    if scenario["name"] == "pre_backend"
                    and scenario["main"] == 1
                    else [finish("Verified 42")]
                )
            categories.append(category)
            response = {
                "id": f"reply-{len(requests)}",
                "model": "scripted",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": text,
                            **({"tool_calls": calls} if calls else {}),
                        },
                        "finish_reason": "tool_calls" if calls else "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 50_000 if category == "main" else 40,
                    "completion_tokens": 10,
                    "total_tokens": 50_010 if category == "main" else 50,
                },
            }
            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            requests,
            categories,
            scenario,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


async def seed_history(store, name):
    if name == "pre_backend":
        return
    messages = []
    for index in range(4):
        messages.extend(
            [
                {
                    "id": f"seed-user-{index}",
                    "role": "user",
                    "content": f"Question {index}. " + "x" * 8_000,
                },
                {
                    "id": f"seed-answer-{index}",
                    "role": "assistant",
                    "content": f"Answer {index}. " + "y" * 8_000,
                },
            ]
        )
    await seed_messages(store, "test", "parity", messages)


def merged_parts(traces):
    """Трасса, пришедшая частями (``trace_part``), — снова одной записью."""
    merged = []
    for record in deepcopy(traces):
        data = record["trace_data"]
        part = data.pop("trace_part", None)
        if part is None or part[0] == 1:
            merged.append(record)
            continue
        target = merged[-1]["trace_data"]
        for key, value in data.items():
            if isinstance(value, list) and isinstance(target.get(key), list):
                target[key] += value
            else:
                target[key] = value
    return merged


def normalized_traces(traces, workspace):
    values = merged_parts(traces)
    for record in values:
        record["id"] = record.pop("newly_summarized_message_id")
        record["trace_data"] = dict(sorted(record["trace_data"].items()))
    return normalize(values, workspace)


@pytest.mark.parametrize(
    "legacy", [False, True], ids=["current", "old_history"]
)
@pytest.mark.parametrize("backend", ["file", "sqlite"])
@pytest.mark.parametrize(
    "name", ["pre_run", "pre_backend", "multibatch", "repair"]
)
async def test_automatic_summary_full_trace_parity(
    tmp_path, monkeypatch, summary_http, backend, name, legacy
):
    binary = temporal_cli()
    from gc_core.config_modules import env_vars

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        env_vars, "env", env_vars.EnvVars(gc_home=tmp_path / "home")
    )
    url, requests, categories, scenario = summary_http
    scenario["name"] = name
    config = WorkerConfig.model_validate(
        {
            "workflow_queue": "summary",
            # Сверка с локальным ходом: тот же набор инструментов.
            "human_input": True,
            "resource_queue": "summary",
            "model_queue": "summary",
            "state_dir": str(tmp_path),
            "thread_stores": {
                "files": {
                    "kind": backend,
                    "path": str(tmp_path / "native_threads"),
                }
            },
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
                            "model": "scripted",
                            "max_steps": 5,
                            # Под Temporal project memory нет.
                            "stop_tools": ["project_memory"],
                            "use_local_subagents": False,
                            "strict_turn_finish_validation": False,
                        },
                        "tui": {"text_answer_mode": "deny"},
                        "logging": {
                            "save_logs": False,
                            "console_trace": False,
                        },
                        "summary": {
                            "chat_completions_summary_trigger_chars": 0
                            if name == "pre_backend"
                            else 30_000,
                            "chat_completions_summary_trigger_tokens": 20_000
                            if name == "pre_backend"
                            else 0,
                            "chat_completions_summary_turn_end_trigger_chars": 0,
                            "chat_completions_summary_turn_end_trigger_tokens": 0,
                            "incremental_batch_chars": 20_000
                            if name == "multibatch"
                            else 1_000_000,
                            "last_dialog_steps_count": 1,
                            "last_dialog_step_message_max_chars": 100,
                        },
                    },
                }
            },
        }
    )
    local_store = FileThreadStore(tmp_path / "local_threads")
    await seed_history(local_store, name)
    local_config = LoadedSettings(
        settings=config.profiles["test"].core,
        runtime=RuntimeEnv(launch_root=str(tmp_path)),
    )
    ctx = await acreate_run_context(local_config, thread_store=local_store)
    if legacy:
        ctx.execution_patch = lambda _patch: False
    traces = []

    async def trace(record):
        traces.append(asdict(record))

    ctx.summary_trace_sink = trace
    try:
        answer = await AgentExecutor(ctx).arun(
            ["Verify the accumulated facts"],
            system="test",
            thread_id="parity",
            message_ids=["input-0"],
        )
        expected = await snapshot(answer, requests, local_store, tmp_path)
        expected_traces = normalized_traces(traces, tmp_path)
        expected_categories = list(categories)
    finally:
        release_run_logger(ctx.logger)
    assert traces and traces[0]["trigger_reason"] == (
        "pre_backend" if name == "pre_backend" else "pre_run"
    )
    batches = traces[0]["trace_data"]["step1_batches"]
    assert len(batches) == (4 if name == "multibatch" else 1)
    if name == "repair":
        assert (
            "repair" in categories and batches[0]["repair"]["repaired_valid"]
        )
    if name == "multibatch":
        assert (
            traces[0]["trace_data"]["memory_patch"]["reason"]
            == "skipped_multi_batch"
        )
    else:
        assert "memory" in categories
    requests.clear()
    categories.clear()
    traces.clear()
    scenario.update(main=0, summary=0)
    shutil.rmtree(tmp_path / "home", ignore_errors=True)
    provider, native, stores = build_runtime(config)
    await seed_history(provider.stores["files"], name)
    original = native._summary_trace

    async def native_trace(call, context):
        traces.append(deepcopy(call.data))
        return await original(call, context)

    native._summary_trace = native_trace
    try:
        async with await WorkflowEnvironment.start_local(
            dev_server_existing_path=binary
        ) as env:
            options = worker_kwargs(config, native, stores)
            if legacy:
                options["patch_activation_callback"] = lambda _patch: False
            async with Worker(env.client, **options):
                handle = await env.client.start_workflow(
                    UniversalAgentWorkflow.run,
                    RunRequest(
                        "test",
                        "files",
                        "test",
                        "parity",
                        ["Verify the accumulated facts"],
                        "summary",
                        "summary",
                        message_ids=["input-0"],
                    ),
                    id="native-summary",
                    task_queue="summary",
                )
                result = await asyncio.wait_for(handle.result(), 45)
                history = await handle.fetch_history()
            actual = await snapshot(
                result["answer"], requests, provider.stores["files"], tmp_path
            )
            (tmp_path / "local-summary.json").write_text(
                json.dumps(expected, ensure_ascii=False, indent=2)
            )
            (tmp_path / "native-summary.json").write_text(
                json.dumps(actual, ensure_ascii=False, indent=2)
            )
            assert categories == expected_categories
            assert actual == expected
            assert normalized_traces(traces, tmp_path) == expected_traces
            count = len(requests)
            await Replayer(workflows=[UniversalAgentWorkflow]).replay_workflow(
                history
            )
            assert len(requests) == count
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "legacy_progress", [False, True], ids=["progress", "old20"]
)
async def test_startup_long_preserved_request_progress_and_replay(
    tmp_path,
    monkeypatch,
    summary_http,
    legacy_progress,
):
    """A 5700-char new request stays whole; startup needs one summary or old 20."""
    binary = temporal_cli()
    from gc_core.config_modules import env_vars

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        env_vars, "env", env_vars.EnvVars(gc_home=tmp_path / "home")
    )
    url, requests, categories, scenario = summary_http
    scenario["name"] = "long_preserved"
    config = WorkerConfig.model_validate(
        {
            "workflow_queue": "startup-summary",
            "resource_queue": "startup-summary",
            "model_queue": "startup-summary",
            "state_dir": str(tmp_path),
            "profiles": {
                "test": {
                    "workspace": str(tmp_path),
                    "core": {
                        "level": "default",
                        "backend": {
                            "api_mode": "chat_completions",
                            "completions_url": url,
                            "chat_completion_kwargs": {"stream": False},
                        },
                        "agent": {
                            "model": "scripted",
                            "max_steps": 5,
                            "stop_tools": ["project_memory"],
                            "use_local_subagents": False,
                            "strict_turn_finish_validation": False,
                        },
                        "logging": {
                            "save_logs": False,
                            "console_trace": False,
                        },
                        "tui": {"text_answer_mode": "deny"},
                        "summary": {
                            "chat_completions_summary_trigger_chars": 2000,
                            "chat_completions_summary_trigger_tokens": 0,
                            "chat_completions_summary_turn_end_trigger_chars": 0,
                            "chat_completions_summary_turn_end_trigger_tokens": 0,
                        },
                    },
                }
            },
        }
    )
    provider, native, stores = build_runtime(config)
    main_inputs = []
    resolve_messages = native._resolve_message_refs

    async def record_main_input(call):
        resolved = await resolve_messages(call)
        if resolved["consumer"] == "main":
            main_inputs.append(deepcopy(resolved))
        return resolved

    native._resolve_message_refs = record_main_input
    try:
        async with await WorkflowEnvironment.start_local(
            dev_server_existing_path=binary
        ) as env:
            options = worker_kwargs(config, native, stores)
            if legacy_progress:
                options["patch_activation_callback"] = lambda item: (
                    item.patch_id != "startup-compaction-progress-v1"
                )
            async with Worker(env.client, **options):
                handle = await env.client.start_workflow(
                    UniversalAgentWorkflow.run,
                    RunRequest(
                        "test",
                        "files",
                        "test",
                        "startup",
                        ["x" * 5700],
                        "startup-summary",
                        "startup-summary",
                        message_ids=["new"],
                    ),
                    id="startup-summary",
                    task_queue="startup-summary",
                )
                result = await asyncio.wait_for(handle.result(), 60)
                history = await handle.fetch_history()
            version = "old20" if legacy_progress else "progress"
            (tmp_path / f"startup-history-{version}.json").write_text(
                history.to_json()
            )
            evidence = {
                "categories": categories,
                "requests": len(requests),
                "new_request_chars": 5700,
                "summary_trigger_chars": 2000,
                "legacy_progress": legacy_progress,
            }
            (tmp_path / "startup-http-evidence.json").write_text(
                json.dumps(evidence)
            )
            assert "Verified 42" in str(result["answer"])
            before_main = categories[: categories.index("main")]
            assert before_main.count("summary") == (
                20 if legacy_progress else 1
            )
            assert before_main.count("memory") == (
                20 if legacy_progress else 1
            )
            assert categories.count("main") == 1
            main = requests[categories.index("main")]
            assert (
                sum(
                    m.get("role") == "user" and m.get("content") == "x" * 5700
                    for m in main["messages"]
                )
                == 1
            )
            assert len(main_inputs) == 1
            canonical = main_inputs[0]["messages"]
            assert sum(m.get("id") == "new" for m in canonical) == 1
            incoming = next(m for m in canonical if m.get("id") == "new")
            assert incoming["role"] == "user"
            assert incoming["content"] == "x" * 5700
            messages = await thread_messages(
                provider.stores["files"], "test", "startup"
            )
            assert sum(m.get("id") == "new" for m in messages) == 1
            assert (
                next(m for m in messages if m.get("id") == "new") == incoming
            )
            from gc_core.message_policy.user_message_policy import (
                is_dialog_summary_user_message_text,
            )

            matches = [
                {
                    "id": m.get("id"),
                    "role": m.get("role"),
                    "content_chars": len(str(m.get("content", ""))),
                    "dialog_summary": is_dialog_summary_user_message_text(
                        str(m.get("content", ""))
                    ),
                }
                for m in canonical
                if "x" * 5700 in str(m.get("content", ""))
            ]
            evidence["matching_canonical_messages"] = matches
            evidence["canonical_new_count"] = sum(
                m.get("id") == "new" for m in canonical
            )
            (tmp_path / "startup-http-evidence.json").write_text(
                json.dumps(evidence)
            )
            count = len(requests)
            await Replayer(workflows=[UniversalAgentWorkflow]).replay_workflow(
                history
            )
            assert len(requests) == count
    finally:
        await provider.aclose()
