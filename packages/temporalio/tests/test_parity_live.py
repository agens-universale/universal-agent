"""Real local executor and native workflow consume identical HTTP scripts.

Reuse the existing develop parity scenarios, never rewrite their reference.
Only opaque identities, timestamps and workspace locations are normalized.
"""

import asyncio
import json
import re
import shutil
import threading
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from live_support import temporal_cli
from gc_core.agent_executor import AgentExecutor
from gc_core.execution.contracts import ToolResult
from gc_core.run_context import acreate_run_context
from gc_core.settings import LoadedSettings, RuntimeEnv
from gc_core.thread_store_file import FileThreadStore
from gc_core.utils.logging_module import release_run_logger
from gc_core_temporalio.contracts import ActivityCall, RunRequest, UserMessage
from gc_core_temporalio.provider import WorkerConfig
from gc_core_temporalio.worker import build_runtime, worker_options
from gc_core_temporalio.workflow import AgentWorkflow
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

CASES = [
    "calculator",
    "finish_after_work",
    "duplicate_finish",
    "invalid_json",
    "invalid_type",
    "dropped_argument",
    "unknown_tool",
    "tool_error",
    "file_roundtrip",
    "finish_judge_accept",
    "finish_judge_reject",
    "project_memory_roundtrip",
    "project_memory_compaction",
    "session_summary",
    "model_http_failure",
    "file_append_backup_mkdir",
    "file_download",
    "stored_output",
    "turn_end_summary",
    "text_deny",
    "text_length",
    "memory_two_turns",
    "human_boundary",
    "queued_before",
    "queued_during",
    "stop_mid_batch",
]

#: Эталонные сценарии ядра, которые под Temporal не гоняются, и почему.
#: Новый сценарий ядра без решения роняет
#: ``test_every_core_scenario_runs_under_temporal_or_is_explained``.
SKIPPED = dict.fromkeys(
    (
        "deferred_accounting_length",
        "deferred_accounting_outcome",
        "deferred_accounting_refusal",
    ),
    "проверяет локальный API отложенного учёта (account_run=False, "
    "aclose_run_accounting); постобработку хода Temporal (format_text) "
    "проверяет test_workflow_controls",
)
# Режим текстового ответа задаёт рантайм поверхности (RuntimeEnv), а профиль
# воркера рантайм пока не принимает. Вернуть в CASES, когда профиль станет
# LoadedSettings (настройки + рантайм).
SKIPPED["text_allow"] = (
    "text_answer_mode=allow задаётся RuntimeEnv поверхности; профиль "
    "воркера его пока не принимает"
)


@pytest.fixture
def parity_scenarios(monkeypatch):
    monkeypatch.syspath_prepend(
        str(Path(__file__).parents[2] / "core" / "tests")
    )
    from gc_testkit.helpers.summary_sample import valid_summary_xml_for_tests
    from parity_harness import assistant_text
    from test_local_runtime_parity import batch, call, finish, scenarios

    cases = scenarios()
    # Калькулятор исполняется в коде workflow, без activity: остановку
    # привязываем к первому инструменту-activity (чтение файла), одинаково
    # для обеих сторон.
    cases["stop_mid_batch"] = {
        "turns": [
            batch(
                call("fs_read_file", {"path": "input.txt"}),
                call("exec_calculator", {"expression": "2+2"}, "not-started"),
            )
        ],
        "stop_after_tool": True,
    }
    cases["project_memory_roundtrip"] = {
        "turns": [
            call(
                "project_memory",
                {"action": "write", "content": "Stable fact A."},
            ),
            call(
                "project_memory",
                {"action": "append", "content": "Stable fact B."},
                "append",
            ),
            call("project_memory", {"action": "read", "content": ""}, "read"),
            finish("Память сохранена"),
        ]
    }
    cases["project_memory_compaction"] = {
        "turns": [
            call("project_memory", {"action": "write", "content": "A" * 60}),
            call(
                "project_memory",
                {"action": "append", "content": "B" * 50},
                "append",
            ),
            assistant_text("Facts A and B retained."),
            call("project_memory", {"action": "read", "content": ""}, "read"),
            finish("Память сжата"),
        ],
        "core": {"project_memory": {"max_chars": 80, "max_append_chars": 100}},
    }
    cases["session_summary"] = {
        "turns": [
            call("exec_calculator", {"expression": "3 * 450"}),
            call(
                "session_compact",
                {"reason": "verified phase complete"},
                "compact",
            ),
            assistant_text(valid_summary_xml_for_tests(seed="TOTAL=1350")),
            assistant_text(
                "<goal>Verify total</goal><constraints></constraints><state></state><knowledge>TOTAL=1350</knowledge>"
            ),
            finish("Сводка сохранена"),
        ]
    }
    cases["turn_end_summary"] = {
        "turns": [
            finish("TOTAL=1350"),
            assistant_text(valid_summary_xml_for_tests(seed="TOTAL=1350")),
            assistant_text(
                "<goal>Verify total</goal><constraints></constraints><state></state><knowledge>TOTAL=1350</knowledge>"
            ),
        ],
        "core": {
            "summary": {"chat_completions_summary_turn_end_trigger_chars": 1}
        },
    }
    cases["model_http_failure"] = {
        "turns": [
            {
                "__http_status__": 503,
                "error": {
                    "message": "provider unavailable",
                    "type": "server_error",
                },
            },
            call("exec_calculator", {"expression": "3 * 450"}),
            finish("Recovered provider"),
        ]
    }
    cases["file_append_backup_mkdir"] = {
        "turns": [
            call(
                "fs_write_file",
                {
                    "path": "nested/result.txt",
                    "content": "first",
                    "make_dirs": True,
                },
            ),
            call("fs_read_file", {"path": "nested/result.txt"}, "read-first"),
            call(
                "fs_append_file",
                {
                    "path": "nested/result.txt",
                    "content": "second",
                    "separator": "\n",
                },
                "append",
            ),
            call("fs_read_file", {"path": "nested/result.txt"}, "read-second"),
            call(
                "fs_write_file",
                {
                    "path": "nested/result.txt",
                    "content": "replacement",
                    "backup": True,
                },
                "call-replace",
            ),
            call(
                "fs_read_file",
                {"path": "nested/result.txt.bak"},
                "read-backup",
            ),
            finish("Backup verified"),
        ]
    }
    cases["file_download"] = {
        "turns": [
            call(
                "download_file",
                {"url": "FIXTURE_URL", "filename": "fixture.txt"},
            ),
            call(
                "fs_read_file",
                {"path": "downloads/fixture.txt"},
                "read-download",
            ),
            finish("Download verified"),
        ]
    }

    def read_stored(request):
        message = next(
            m for m in reversed(request["messages"]) if m.get("role") == "tool"
        )
        envelope = json.loads(message["content"])
        assert envelope["stored"] and envelope["truncated"]
        return call(
            "session_read_stored_tool_output",
            {
                "output_ref": envelope["output_ref"],
                "offset": envelope["next_offset"],
                "limit": 500,
            },
            "retrieve",
        )

    cases["stored_output"] = {
        "turns": [
            call("fs_read_file", {"path": "big.txt"}, "read-big"),
            read_stored,
            finish("Stored output retrieved"),
        ],
        "agent_extra": {"tool_output_inline_max_chars": 500},
    }
    return cases


#: Ввод до хода и посреди хода — те же тексты, что у эталона ядра.
QUEUED_BEFORE = {
    "id": "queued-before",
    "role": "user",
    "content": "Не забудь проверить итог",
}
QUEUED_DURING = {
    "id": "queued-during",
    "role": "user",
    "content": "Уточнение: ответ на русском",
}


def on_first_request(script, action):
    """Перед ответом на первый запрос к модели выполнить ``action``."""
    first = script[0]

    def respond(request):
        action()
        return first(request) if callable(first) else first

    script[0] = respond


async def drive_session(handle, messages):
    """Довести сессию: ответить человеку, отправить следующие сообщения,
    закрыть живую сессию. Ответы ходов — по мере их завершения."""
    answers, questions, answered = [], [], set()
    sent = 1
    result = asyncio.ensure_future(handle.result())
    # Живая сессия делает Continue-As-New после хода: запросы и updates —
    # к последнему run, а не к первому, к которому привязан ``handle``.
    handle = handle._client.get_workflow_handle(handle.id)
    try:
        while not result.done():
            status = await handle.query(AgentWorkflow.status)
            pending = status.get("pending_human")
            if pending and pending["request_id"] not in answered:
                answered.add(pending["request_id"])
                questions.append(pending.get("question"))
                await handle.execute_update(
                    AgentWorkflow.answer_human,
                    args=[pending["request_id"], "1350"],
                )
            if len(messages) > 1 and status.get("turns", 0) >= sent:
                if len(answers) < sent:
                    answers.append(status.get("answer"))
                if sent < len(messages):
                    await handle.execute_update(
                        AgentWorkflow.submit_message,
                        UserMessage(f"input-{sent}", messages[sent]),
                    )
                    sent += 1
                else:
                    await handle.signal(AgentWorkflow.close_session)
            await asyncio.wait({result}, timeout=0.2)
        value = result.result()
    finally:
        result.cancel()
    if len(messages) == 1:
        answers.append(value["answer"])
    return value, answers, questions


def test_every_core_scenario_runs_under_temporal_or_is_explained(
    parity_scenarios,
):
    names = set(parity_scenarios)
    covered = set(CASES) | set(SKIPPED)
    assert names <= covered, (
        "Новые эталонные сценарии ядра не гоняются под Temporal: добавьте в "
        f"CASES или в SKIPPED с причиной: {sorted(names - covered)}"
    )
    assert covered <= names, sorted(covered - names)
    assert not set(CASES) & set(SKIPPED)


@pytest.fixture
def parity_http():
    scripts, requests, errors = [], [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            body = b"Downloaded fixture = 42\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            request = json.loads(
                self.rfile.read(int(self.headers["content-length"]))
            )
            requests.append(request)
            if not scripts:
                errors.append(request)
                self.send_error(500, "Parity script exhausted")
                return
            response = scripts.pop(0)
            if callable(response):
                response = response(request)
            status = response.pop("__http_status__", 200)
            body = json.dumps(response).encode()
            self.send_response(status)
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
            scripts,
            requests,
            errors,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def normalize(trace, workspace):
    volatile = {
        "created_at",
        "updated_at",
        "timestamp",
        "ts",
        "elapsed_ms",
        "duration_ms",
    }
    ids = {}
    identity = {
        "id",
        "message_id",
        "response_id",
        "previous_response_id",
        "state_id",
        "output_ref",
    }

    def collect(value):
        if isinstance(value, dict):
            # По имени ключа, а не по порядку вставки: SQLite и файлы
            # возвращают ключи сообщения в разном порядке.
            for key, item in sorted(value.items()):
                if key in identity and isinstance(item, str):
                    ids.setdefault(item, f"<id-{len(ids)}>")
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, str) and value.startswith(("{", "[")):
            try:
                collect(json.loads(value))
            except (ValueError, TypeError):
                pass

    collect(trace)

    def visit(value):
        if isinstance(value, dict):
            return {
                key: visit(item)
                for key, item in value.items()
                if key not in volatile
            }
        if isinstance(value, list):
            return [visit(item) for item in value]
        if isinstance(value, str):
            value = value.replace(str(workspace), "<workspace>")
            for key in sorted(ids, key=len, reverse=True):
                value = value.replace(key, ids[key])
            value = re.sub(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)",
                "<timestamp>",
                value,
            )
            if value.startswith(("{", "[")):
                try:
                    return visit(json.loads(value))
                except (ValueError, TypeError):
                    pass
            return value
        return value

    return visit(trace)


def snapshot(answer, requests, store, workspace):
    messages = store.load_messages("test", "parity")
    meta = store.load_meta("test", "parity")
    return normalize(
        {
            "answer": answer,
            "requests": deepcopy(requests),
            "messages": messages,
            "shared": meta.get("shared"),
            "token_usage": meta.get("token_usage"),
            "files": {
                name: (workspace / name).read_text()
                for name in (
                    "result.txt",
                    ".gc/project_memory.md",
                    "nested/result.txt",
                    "nested/result.txt.bak",
                    "downloads/fixture.txt",
                )
                if (workspace / name).exists()
            },
        },
        workspace,
    )


@pytest.mark.parametrize("name", CASES)
@pytest.mark.parametrize("backend", ["file", "sqlite"])
async def test_actual_http_local_native_parity(
    tmp_path, monkeypatch, parity_scenarios, parity_http, name, backend
):
    binary = temporal_cli()
    monkeypatch.chdir(tmp_path)
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(
        env_vars, "env", env_vars.EnvVars(gc_home=tmp_path / "home")
    )
    url, script, requests, errors = parity_http
    scenario = deepcopy(parity_scenarios[name])
    (tmp_path / "input.txt").write_text("3 items at 450 each\n")
    (tmp_path / "big.txt").write_text("Stored payload. " * 200)
    prompt = tmp_path / "system.md"
    prompt.write_text("SYSTEM PROMPT")
    for response in scenario["turns"]:
        if callable(response):
            continue
        for tool in (response.get("choices") or [{"message": {}}])[0][
            "message"
        ].get("tool_calls", []):
            function = tool["function"]
            if function["name"] in {
                "fs_read_file",
                "fs_write_file",
                "fs_append_file",
            }:
                arguments = json.loads(function["arguments"])
                arguments["path"] = str(tmp_path / arguments["path"])
                function["arguments"] = json.dumps(arguments)
            elif function["name"] == "download_file":
                arguments = json.loads(function["arguments"])
                arguments["url"] = (
                    url.removesuffix("/v1/chat/completions") + "/fixture.txt"
                )
                function["arguments"] = json.dumps(arguments)
    config = WorkerConfig.model_validate(
        {
            "workflow_queue": "parity",
            "resource_queue": "parity",
            "model_queue": "parity",
            "state_dir": str(tmp_path),
            "thread_stores": {
                "files": {
                    "kind": backend,
                    "path": str(
                        tmp_path
                        / (
                            "native_threads"
                            if backend == "file"
                            else "native_threads.sqlite"
                        )
                    ),
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
                            "model": "parity-model",
                            "max_steps": 15,
                            "strict_turn_finish_validation": scenario.get(
                                "config", {}
                            ).get(
                                "agent.strict_turn_finish_validation", False
                            ),
                            **scenario.get("agent_extra", {}),
                        },
                        "tui": {
                            "text_answer_mode": scenario.get("config", {}).get(
                                "text_answer_mode", "deny"
                            )
                        },
                        "paths": {
                            "chat_completions_system_prompt_path": str(prompt)
                        },
                        "logging": {
                            "save_logs": False,
                            "console_trace": False,
                        },
                        **scenario.get("core", {}),
                    },
                }
            },
        }
    )
    local_config = LoadedSettings(
        settings=config.profiles["test"].core,
        runtime=RuntimeEnv(launch_root=str(tmp_path)),
    )
    local_store = FileThreadStore(str(tmp_path / "local_threads"))
    local_ctx = await acreate_run_context(
        local_config, thread_store=local_store
    )
    messages = scenario.get("messages", ["Выполни контрольную задачу"])
    loop = asyncio.get_running_loop()
    executor = AgentExecutor(local_ctx)
    local_questions = []

    async def ask(question):
        local_questions.append(question)
        return "1350"

    local_ctx.ask_human = ask
    if scenario.get("queued_before"):
        executor.add_message(dict(QUEUED_BEFORE))
    script.extend(deepcopy(scenario["turns"]))
    if scenario.get("queued_during"):

        async def queue_locally():
            # Как сообщение клиента: его ID — ``message_id`` (дедупликация
            # повторов), свой ``id`` сообщению даёт ядро.
            executor.add_message(
                {
                    "role": "user",
                    "content": QUEUED_DURING["content"],
                    "message_id": QUEUED_DURING["id"],
                }
            )

        on_first_request(
            script,
            lambda: asyncio.run_coroutine_threadsafe(
                queue_locally(), loop
            ).result(30),
        )
    if scenario.get("stop_after_tool"):
        import gc_core.processing_responses as pr

        real_apply = pr.aapply_tool

        async def apply_then_stop(*args, **kwargs):
            output = await real_apply(*args, **kwargs)
            executor.stop_run()
            return output

        monkeypatch.setattr(pr, "aapply_tool", apply_then_stop)
    try:
        local_answers = []
        for number, message in enumerate(messages):
            local_answers.append(
                await executor.arun(
                    [message],
                    system="test",
                    thread_id="parity",
                    message_ids=[f"input-{number}"],
                )
            )
        assert not script and not errors
        local = snapshot(local_answers[-1], requests, local_store, tmp_path)
    finally:
        release_run_logger(local_ctx.logger)
        if scenario.get("stop_after_tool"):
            monkeypatch.setattr(pr, "aapply_tool", real_apply)
    requests.clear()
    (tmp_path / "result.txt").unlink(missing_ok=True)
    shutil.rmtree(tmp_path / ".gc", ignore_errors=True)
    shutil.rmtree(tmp_path / "home", ignore_errors=True)
    shutil.rmtree(tmp_path / "nested", ignore_errors=True)
    shutil.rmtree(tmp_path / "downloads", ignore_errors=True)
    script.extend(deepcopy(scenario["turns"]))
    contexts, native, threads = build_runtime(config)
    session = {}
    if scenario.get("stop_after_tool"):
        original_tool = native.tool

        @activity.defn(name="agent.tool.execute")
        async def tool_then_stop(call: ActivityCall) -> ToolResult:
            result = await original_tool(call)
            if not session.get("stopped"):
                # Сигнал — до завершения activity: workflow примет stop
                # раньше её результата, как local stop_run после инструмента.
                session["stopped"] = True
                await session["handle"].signal(AgentWorkflow.stop)
            return result

        native.tool = tool_then_stop
    first = [messages[0]]
    first_ids = ["input-0"]
    if scenario.get("queued_before"):
        first.append(QUEUED_BEFORE["content"])
        first_ids.append(QUEUED_BEFORE["id"])
    try:
        async with await WorkflowEnvironment.start_local(
            dev_server_existing_path=binary
        ) as env:
            (options,) = worker_options(config, native, threads)
            async with Worker(
                env.client,
                **options,
            ):
                handle = await env.client.start_workflow(
                    AgentWorkflow.run,
                    RunRequest(
                        "test",
                        "files",
                        "test",
                        "parity",
                        first,
                        "parity",
                        "parity",
                        message_ids=first_ids,
                        keep_alive=len(messages) > 1,
                    ),
                    id="native-parity",
                    task_queue="parity",
                )
                session["handle"] = handle
                if scenario.get("queued_during"):
                    on_first_request(
                        script,
                        lambda: asyncio.run_coroutine_threadsafe(
                            handle.execute_update(
                                AgentWorkflow.submit_message,
                                UserMessage(
                                    QUEUED_DURING["id"],
                                    QUEUED_DURING["content"],
                                ),
                            ),
                            loop,
                        ).result(30),
                    )
                (
                    result,
                    native_answers,
                    native_questions,
                ) = await asyncio.wait_for(drive_session(handle, messages), 60)
                history = await env.client.get_workflow_handle(
                    "native-parity"
                ).fetch_history()
            assert not script and not errors
            assert native_answers == local_answers
            assert len(native_questions) == len(local_questions)
            actual = snapshot(
                result["answer"], requests, contexts.stores["files"], tmp_path
            )
            (tmp_path / "local-trace.json").write_text(
                json.dumps(local, ensure_ascii=False, indent=2)
            )
            (tmp_path / "native-trace.json").write_text(
                json.dumps(actual, ensure_ascii=False, indent=2)
            )
            assert actual == local
            if name == "stored_output":
                outputs = [
                    m["output"]
                    for m in actual["messages"]
                    if m.get("type") == "function_call_output"
                ]
                assert outputs[0]["stored"] and outputs[0]["truncated"]
                assert "Stored payload." in outputs[1]["content"]
                assert outputs[1]["offset"] == 500
            if name == "file_append_backup_mkdir":
                assert actual["files"]["nested/result.txt"] == "replacement"
                assert (
                    actual["files"]["nested/result.txt.bak"] == "first\nsecond"
                )
            if name == "file_download":
                assert (
                    actual["files"]["downloads/fixture.txt"]
                    == "Downloaded fixture = 42\n"
                )
            if name == "project_memory_roundtrip":
                assert (
                    "Stable fact A."
                    in actual["files"][".gc/project_memory.md"]
                )
                assert (
                    "Stable fact B."
                    in actual["files"][".gc/project_memory.md"]
                )
            if name == "project_memory_compaction":
                assert (
                    actual["files"][".gc/project_memory.md"]
                    == "Facts A and B retained."
                )
            if name in {"session_summary", "turn_end_summary"}:
                assert any(
                    "dialog_summary" in str(message)
                    for message in actual["messages"]
                )
                assert (
                    "TOTAL=1350"
                    in actual["shared"]["working_memory"]["knowledge"]
                )
            count = len(requests)
            await Replayer(workflows=[AgentWorkflow]).replay_workflow(history)
            assert len(requests) == count
    finally:
        await contexts.aclose()
