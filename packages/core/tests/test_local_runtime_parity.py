"""Legacy routing execution preserves develop's requests, results and persisted state.

Only model HTTP responses are scripted; the executor, loop, tools, finish gate,
serialization and thread store run normally. Reference digests are captured by
running this same harness against the recorded develop revision, never by
accepting snapshots from the implementation under test.
"""

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path

import pytest
from gc_core import processing_responses as pr
from gc_core.agent_executor import AgentExecutor
from gc_core.config_modules.strip_tags_projection import (
    projection_tags_from_config,
)
from gc_core.intent_log import operations as intent_operations
from gc_core.persistence.live_volume import live_volume_from_messages
from gc_core.run_context import acreate_run_context
from gc_core.thread_store import _thread_paths, resolve_threads_dir
from gc_core.utils.llm_chat_client import arequest_chat_completion_outcome
from gc_core.utils.logging_module import release_run_logger
from parity_harness import (
    ScriptedBackend,
    assistant_text,
    assistant_tool_call,
    install,
    judge_accept,
    parity_config,
)

REFERENCE = (
    Path(__file__).parent / "fixtures/local_runtime_reference_320ed0b19.json"
)

#: Ключи meta, которые добавляет хранилище v2 (спека
#: docs/superpowers/specs/2026-10-14-agent-storage-v2-design.md, 5.7). Эталон
#: develop их не знает, поэтому сравнение вырезает их — по списку, не по маске:
#: любой другой новый ключ обязан уронить паритет.
V2_ADDED_META_KEYS = (("workspace",), ("runtime", "turn"))


def strip_v2_added_keys(meta):
    out = deepcopy(meta)
    for path in V2_ADDED_META_KEYS:
        parent = out
        for part in path[:-1]:
            parent = parent.get(part)
            if not isinstance(parent, dict):
                break
        else:
            parent.pop(path[-1], None)
    # Секцию, опустевшую после вырезания, develop не пишет вовсе.
    if out.get("runtime") == {}:
        out.pop("runtime")
    return out


def call(name, arguments, call_id="c1"):
    return assistant_tool_call(name, arguments, call_id)


def finish(report="Проверка завершена", call_id="finish"):
    return call(
        "turn_finish",
        {
            "solution_type": "full_solution",
            "task": "Выполнить контрольную задачу",
            "task_report": report,
            "handoff": "{}",
        },
        call_id,
    )


def batch(*turns):
    response = deepcopy(turns[0])
    response["choices"][0]["message"]["tool_calls"] = [
        tool
        for turn in turns
        for tool in turn["choices"][0]["message"]["tool_calls"]
    ]
    return response


def scenarios():
    calc = call("exec_calculator", {"expression": "3 * 450"})
    bad_json = call("exec_calculator", {})
    bad_json["choices"][0]["message"]["tool_calls"][0]["function"][
        "arguments"
    ] = '{"expression":'
    truncated = assistant_text("Незавершённый ответ")
    truncated["choices"][0]["finish_reason"] = "length"
    refused = assistant_text("Отказ провайдера")
    refused["choices"][0]["finish_reason"] = "content_filter"
    return {
        # New develop contract: auxiliary outcome and usage can arrive after
        # the loop, before the executor's one final accounting write.
        "deferred_accounting_outcome": {
            "turns": [calc, finish(), assistant_text('{"total":1350}')],
            "post_run_outcome": "stop",
        },
        "deferred_accounting_length": {
            "turns": [calc, finish(), truncated],
            "post_run_outcome": "length",
        },
        "deferred_accounting_refusal": {
            "turns": [calc, finish(), refused],
            "post_run_outcome": "content_filter",
        },
        "calculator": {"turns": [calc, finish("Итого 1350")]},
        "finish_after_work": {"turns": [batch(finish(), calc)]},
        "duplicate_finish": {
            "turns": [batch(finish(), calc, finish("Лишний", "finish-2"))]
        },
        "memory_two_turns": {
            "turns": [
                call(
                    "session_memory",
                    {
                        "action": "append",
                        "register": "knowledge",
                        "content": "TOTAL=1350",
                    },
                ),
                call(
                    "session_todo",
                    {
                        "todos": [
                            {
                                "content": "Сохранить итог",
                                "status": "completed",
                            }
                        ]
                    },
                    "todo",
                ),
                finish("Итог сохранён"),
                call(
                    "session_memory",
                    {"action": "read", "register": "knowledge"},
                    "read-memory",
                ),
                finish("Из памяти: 1350", "finish-next"),
            ],
            "messages": [
                "Сохрани итог и отметь план",
                "Прочитай сохранённый итог",
            ],
        },
        "invalid_json": {"turns": [bad_json, calc, finish()]},
        "invalid_type": {
            "turns": [
                call("exec_calculator", {"expression": []}),
                calc,
                finish(),
            ]
        },
        "dropped_argument": {
            "turns": [
                call(
                    "exec_calculator",
                    {"expression": "3 * 450", "unrecognized": "value"},
                ),
                finish(),
            ]
        },
        "unknown_tool": {
            "turns": [call("does_not_exist", {}), calc, finish()]
        },
        "tool_error": {
            "turns": [
                call("exec_calculator", {"expression": "1/0"}),
                calc,
                finish(),
            ]
        },
        "text_allow": {
            "turns": [assistant_text("Ответ: 1350")],
            "config": {"text_answer_mode": "allow"},
        },
        "text_deny": {
            "turns": [assistant_text("Преждевременный ответ"), calc, finish()]
        },
        "text_length": {
            "turns": [truncated, calc, finish()],
            "config": {"text_answer_mode": "allow"},
        },
        "finish_judge_accept": {
            "turns": [calc, finish(), judge_accept()],
            "config": {"agent.strict_turn_finish_validation": True},
        },
        "finish_judge_reject": {
            "turns": [
                finish("Пока не проверено"),
                assistant_text(
                    '{"action":"fix_report_and_continue","trace_ok":false,"report_ok":false,"is_question":false,"trace_gaps":["Проверь вычисление"]}'
                ),
                calc,
                finish("Проверено: 1350", "finish-fixed"),
                judge_accept(),
            ],
            "config": {"agent.strict_turn_finish_validation": True},
        },
        "human_boundary": {
            "turns": [
                batch(
                    call(
                        "turn_ask_human", {"question": "Какой итог сохранить?"}
                    ),
                    calc,
                ),
                finish("Ответ человека: 1350"),
            ]
        },
        "queued_before": {"turns": [calc, finish()], "queued_before": True},
        "queued_during": {"turns": [calc, finish()], "queued_during": True},
        "stop_mid_batch": {
            "turns": [
                batch(
                    calc,
                    call(
                        "exec_calculator", {"expression": "2+2"}, "not-started"
                    ),
                )
            ],
            "stop_after_tool": True,
        },
        "file_roundtrip": {
            "turns": [
                call("fs_read_file", {"path": "input.txt"}),
                call(
                    "fs_write_file",
                    {"path": "result.txt", "content": "TOTAL=1350\n"},
                    "write",
                ),
                call("fs_read_file", {"path": "result.txt"}, "verify"),
                finish("Файл проверен: 1350"),
            ]
        },
    }


def normalize(value, workspace, ids=None):
    """Normalize locations/clocks/opaque IDs, retaining call/output identity."""
    ids = {} if ids is None else ids
    volatile = {
        "created_at",
        "updated_at",
        "timestamp",
        "ts",
        "elapsed_ms",
        "duration_ms",
    }
    identity = {
        "id",
        "message_id",
        "response_id",
        "previous_response_id",
        "last_backend_response_id",
        "state_id",
    }
    if isinstance(value, dict):
        if "by_category" in value and "by_model" in value:
            # Новый разрез по моделям проверяется отдельно от неизменённого
            # эталона develop: суммарные токены и вызовы обязаны совпадать.
            fields = (
                "input",
                "output",
                "cached",
                "calls",
                "missing_usage_calls",
            )
            for field in fields:
                assert sum(
                    bucket.get(field, 0)
                    for bucket in value["by_model"].values()
                ) == sum(
                    bucket.get(field, 0)
                    for bucket in value["by_category"].values()
                )
            categories = {}
            attributed_fields = (
                *fields,
                "billed_calls",
                "billed_input",
                "billed_output",
                "billed_cached",
            )
            for category, bucket in value["by_category"].items():
                attributed = bucket.get("by_model")
                if isinstance(attributed, dict):
                    assert set(attributed) <= set(value["by_model"])
                    for field in attributed_fields:
                        assert sum(
                            part.get(field, 0) for part in attributed.values()
                        ) == bucket.get(field, 0)
                categories[category] = {
                    key: item
                    for key, item in bucket.items()
                    if key != "by_model"
                }
            for model, bucket in value["by_model"].items():
                for field in fields:
                    assert sum(
                        category.get("by_model", {})
                        .get(model, {})
                        .get(field, 0)
                        for category in value["by_category"].values()
                    ) == bucket.get(field, 0)
            value = {
                key: item for key, item in value.items() if key != "by_model"
            }
            value["by_category"] = categories
        result = {}
        for key, item in value.items():
            if key in volatile:
                continue
            if key in identity and isinstance(item, str):
                item = ids.setdefault(item, f"opaque-{len(ids) + 1}")
            result[key] = normalize(item, workspace, ids)
        return result
    if isinstance(value, list):
        return [normalize(item, workspace, ids) for item in value]
    if isinstance(value, str):
        value = value.replace(str(workspace), "<workspace>")
        if value.startswith(("{", "[")):
            try:
                return normalize(json.loads(value), workspace, ids)
            except (ValueError, TypeError):
                pass
        # IDs appear in rendered intent/trace XML as well as structured fields.
        return re.sub(
            r"\b(?:m|fco|fc|m_sys)_[0-9a-f]{64}\b",
            "<generated-message-id>",
            value,
        )
    return value


async def run_scenario(
    name,
    workspace,
    monkeypatch,
    *,
    streaming=False,
    legacy_routing_contract=False,
):
    monkeypatch.setattr(
        intent_operations, "utc_now_ts", lambda: "2026-01-01T00:00:00Z"
    )
    scenario = deepcopy(scenarios()[name])
    config = {
        "agent.max_steps": 15,
        "agent.strict_turn_finish_validation": False,
        "text_answer_mode": "deny",
        # Intent-scope выключен: ключа ``enabled`` в схеме нет, активность —
        # непустой ``intent_scope.tools``.
        "intent_scope.tools": {},
        "tui.stream": streaming,
        **scenario.get("config", {}),
    }
    cfg = parity_config(workspace, monkeypatch, **config)
    for turn in scenario["turns"]:
        for tool in turn["choices"][0]["message"].get("tool_calls", []):
            function = tool["function"]
            if function["name"] in {"fs_read_file", "fs_write_file"}:
                arguments = json.loads(function["arguments"])
                arguments["path"] = str(workspace / arguments["path"])
                function["arguments"] = json.dumps(arguments)
    (workspace / "input.txt").write_text(
        "3 items at 450 each\n", encoding="utf-8"
    )
    monkeypatch.chdir(workspace)
    backend = install(monkeypatch, ScriptedBackend(scenario["turns"]))
    ctx = await acreate_run_context(cfg)
    if legacy_routing_contract:
        # Frozen develop references predate the persisted finish outcome.
        ctx.routing_contract_v2 = False
    executor = AgentExecutor(ctx)
    deltas = []
    human_questions = []
    if streaming:
        # Стрим включает явный ``tui.stream`` (выше); ``backend.stream`` после
        # сборки контекста настроек уже не меняет.
        ctx.on_stream_delta = lambda kind, text: deltas.append([kind, text])

    async def ask(question):
        human_questions.append(question)
        return "1350"

    ctx.ask_human = ask
    if scenario.get("queued_before"):
        executor.add_message(
            {
                "id": "queued-before",
                "role": "user",
                "content": "Не забудь проверить итог",
            }
        )
    if scenario.get("queued_during"):
        original_next = backend.next_payload

        def next_with_input(payload):
            if not backend.calls:
                executor.add_message(
                    {
                        "id": "queued-during",
                        "role": "user",
                        "content": "Уточнение: ответ на русском",
                    }
                )
            return original_next(payload)

        monkeypatch.setattr(backend, "next_payload", next_with_input)
    if scenario.get("stop_after_tool"):
        real_apply = pr.aapply_tool

        async def apply_then_stop(*args, **kwargs):
            output = await real_apply(*args, **kwargs)
            executor.stop_run()
            return output

        monkeypatch.setattr(pr, "aapply_tool", apply_then_stop)
    answers = []
    post_run_outcome = None
    try:
        for number, message in enumerate(
            scenario.get("messages", ["Выполни контрольную задачу"])
        ):
            answers.append(
                await executor.arun(
                    [message],
                    thread_id="parity",
                    system="cli",
                    start_new_thread=number == 0,
                    message_ids=[f"input-{number}"],
                    account_run="post_run_outcome" not in scenario,
                )
            )
        if "post_run_outcome" in scenario:
            state = executor.get_state()
            outcome = await arequest_chat_completion_outcome(
                ctx=ctx,
                state=state,
                messages=[
                    {"role": "system", "content": "Format the turn result."},
                    {"role": "user", "content": str(answers[-1])},
                ],
                max_tokens=100,
                consumer="summary",
                request_label="structured_output",
            )
            assert outcome.finish_reason == scenario["post_run_outcome"]
            post_run_outcome = {
                "text": outcome.text,
                "finish_reason": outcome.finish_reason,
            }
            await executor.aclose_run_accounting(console=False)
            _, final_meta_path = _thread_paths(
                resolve_threads_dir(cfg), "cli", "parity"
            )
            first_close = Path(final_meta_path).read_bytes()
            await executor.aclose_run_accounting(console=False)
            assert Path(final_meta_path).read_bytes() == first_close
        backend.assert_clean()
        assert (
            any(payload.get("stream") for payload in backend.calls)
            == streaming
        )
        state = executor.get_state()
        data_path, meta_path = _thread_paths(
            resolve_threads_dir(cfg), "cli", "parity"
        )
        records = [
            json.loads(line)
            for line in Path(data_path).read_text().splitlines()
            if line.strip()
        ]
        assert records, "Сравнение пустых тредов не проверяет поведение"
        meta = json.loads(Path(meta_path).read_text())
        if post_run_outcome is not None:
            usage = meta["token_usage"]["by_category"]["structured_output"]
            assert usage["input"] == 12
            assert usage["output"] == 2
        payloads = [record["payload"] for record in records]
        volume = live_volume_from_messages(
            payloads, tags=pr.projection_tags_from_config(cfg)
        )
        assert all(
            meta["runtime"][key] == value for key, value in volume.items()
        )
        # An absolute temporary directory changes the character count. First
        # validate the real counter above, then compare the same canonical paths.
        canonical_payloads = json.loads(
            json.dumps(payloads).replace(str(workspace), "<workspace>")
        )
        meta["runtime"].update(
            live_volume_from_messages(
                canonical_payloads, tags=projection_tags_from_config(cfg)
            )
        )
        tool_calls = [
            row["payload"]
            for row in records
            if row["payload"].get("type") == "function_call"
        ]
        outputs = [
            row["payload"]
            for row in records
            if row["payload"].get("type") == "function_call_output"
        ]
        assert sorted(call["call_id"] for call in tool_calls) == sorted(
            output["call_id"] for output in outputs
        ), "Нарушены пары call/output"
        if name == "file_roundtrip":
            assert (workspace / "result.txt").read_text() == "TOTAL=1350\n"
        if name == "memory_two_turns":
            assert "TOTAL=1350" in state.shared.working_memory["knowledge"]
            assert state.session_todo[0]["status"] == "completed"
        if name == "stop_mid_batch":
            assert state.tool_calls == 1
        if name == "human_boundary":
            assert len(human_questions) == 1
        trace = normalize(
            {
                "answers": answers,
                "requests": backend.calls,
                "thread": records,
                "meta": strip_v2_added_keys(meta),
                "state": {
                    "messages": state.messages,
                    "steps": state.steps,
                    "tool_calls": state.tool_calls,
                    "turn_tool_calls": state.turn_tool_calls,
                    "memory": state.shared.working_memory,
                    "todo": state.session_todo,
                    "usage": state.usage.to_meta(),
                    "terminal": executor.last_terminal_outcome,
                },
                "human_questions": human_questions,
                "stream_deltas": deltas,
                "post_run_outcome": post_run_outcome,
            },
            workspace,
        )
        summary = {
            key: hashlib.sha256(
                json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
            for key, value in trace.items()
        }
        summary["model_requests"] = len(backend.calls)
        summary["tool_calls"] = state.tool_calls
        summary["recorded_tool_calls"] = len(tool_calls)
        summary["steps"] = state.steps
        return summary, trace
    finally:
        release_run_logger(ctx.logger)


@pytest.mark.parametrize("name", list(scenarios()))
@pytest.mark.parametrize("streaming", [False, True], ids=["json", "sse"])
async def test_legacy_routing_runtime_matches_develop(
    name, streaming, tmp_path, monkeypatch
):
    summary, _ = await run_scenario(
        name,
        tmp_path,
        monkeypatch,
        streaming=streaming,
        legacy_routing_contract=True,
    )
    reference = json.loads(REFERENCE.read_text())
    key = f"{name}:{'sse' if streaming else 'json'}"
    assert summary == reference["scenarios"][key]


def test_strip_removes_only_listed_keys():
    meta = {
        "workspace": "/w",
        "runtime": {"turn": "idle", "live_msg_count": 3},
        "thread_id": "t",
    }
    assert strip_v2_added_keys(meta) == {
        "runtime": {"live_msg_count": 3},
        "thread_id": "t",
    }
    assert meta["workspace"] == "/w"  # вход не мутируется


def test_strip_is_noop_without_new_keys():
    meta = {"runtime": {"live_msg_count": 3}, "thread_id": "t"}
    assert strip_v2_added_keys(meta) == meta


def test_strip_drops_emptied_runtime():
    # develop никогда не пишет пустую секцию runtime: снятие последнего
    # флага убирает её целиком (контракт thread store).
    assert strip_v2_added_keys({"runtime": {"turn": "idle"}}) == {}
