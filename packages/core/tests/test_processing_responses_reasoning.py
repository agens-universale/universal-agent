"""Эпизод мысли главного вызова в цикле агента: реальные RunContext и
Router, замещён только транспорт chat/completions. Запись идёт хуком
``ctx.record_reasoning`` и только для принятого ответа."""

import json

from gc_core import processing_responses as pr
from gc_core import run_context as rc
from gc_core.config_modules.root import AppConfig
from gc_core.execution.environment import utc_now
from gc_core.persistence.reasoning_record import ReasoningRecord
from gc_core.settings import RuntimeEnv
from gc_core.states import State
from gc_core.turn_boundary import begin_turn
from gc_testkit import run_coro

RAW = {
    "models": {"fast": {}, "mid": {}, "big": {}},
    "slots": {"light": "fast", "default": "mid", "max": "big"},
    "backend": {"api_mode": "chat_completions"},
    "model": "auto",
    "logging": {"save_logs": False},
    "tui": {"stream": True},
}


def _ctx(level, *, record_reasoning=True, hook=True, strip_think=False):
    raw = dict(RAW)
    raw["strip_tags_projection"] = {"strip_think": strip_think}
    raw["agent"] = {
        "auto_routing_safe_level": "default",
        "auto_routing_two_level": {"weak_steps": 10, "strong_steps": 3},
    }
    ctx = run_coro(
        rc.acreate_run_context(
            AppConfig.model_validate(raw),
            runtime=RuntimeEnv(record_reasoning=record_reasoning),
            choice_provider=lambda: level,
        )
    )
    deltas, records = [], []
    ctx.on_stream_delta = lambda kind, text: deltas.append((kind, text))
    if hook:

        async def record(rec):
            records.append(rec)

        ctx.record_reasoning = record
    return ctx, deltas, records


def _state(ctx):
    s = State(
        verbose=False,
        model="m",
        tools_spec=ctx.tools_spec,
        tools_descr=ctx.tools_description,
        skills_list=[],
        max_steps=50,
        system_prompt_id="v1",
        use_server_subagents=False,
    )
    s.backend_api_mode = "chat_completions"
    s.messages = [{"role": "system", "content": "SYS", "id": "m_sys"}]
    ctx.attach_state(s)
    begin_turn(s, ctx)
    return s


def _read_call(call_id):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "fs_read_file",
            "arguments": json.dumps({"path": f"src/{call_id}.py"}),
        },
    }


def _completion(n, *, reasoning, tool=True):
    message = {"role": "assistant", "content": "" if tool else "готово"}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    if tool:
        message["tool_calls"] = [_read_call(f"c{n}")]
    return {
        "id": f"chatcmpl-{n}",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls" if tool else "stop",
                "message": message,
            }
        ],
    }


THINK_SEC = 2.5


def _patch_transport(monkeypatch, replies):
    """``replies`` — по одному ответу chat/completions на каждый вызов;
    фаза мысли каждого вызова длится ровно ``THINK_SEC`` по монотонике
    рантайма."""
    sent = []
    now = [100.0]
    monkeypatch.setattr(
        pr, "turn_clock_sources", lambda _ctx: (lambda: now[0], utc_now)
    )

    async def fake_policy(
        *, payload_factory, initial_max_tokens, on_delta=None, **_kw
    ):
        sent.append(payload_factory(initial_max_tokens)["model"])
        reply = replies.pop(0)
        if on_delta is not None:
            on_delta("reset", "")
            on_delta("think", "…")
            now[0] += THINK_SEC
            on_delta("answer", "текст")
            on_delta("tool", "{")
        return reply, {"max_tokens": initial_max_tokens}

    monkeypatch.setattr(
        pr, "arequest_chat_completions_with_policy", fake_policy
    )
    return sent


def _ask(s, ctx):
    return run_coro(pr.aprocess_user_ask([], s, ctx))


def _resets(deltas):
    return [text for kind, text in deltas if kind == "reset"]


def test_accepted_response_episode_is_recorded_once(monkeypatch):
    ctx, deltas, records = _ctx("default")
    s = _state(ctx)
    _patch_transport(monkeypatch, [_completion(1, reasoning="план")])
    _ask(s, ctx)
    [attempt] = _resets(deltas)
    assert attempt.startswith("att_")
    [rec] = records
    assert rec.text == "план"
    assert rec.attempt == attempt
    assert rec.duration_sec == THINK_SEC
    assert getattr(ctx, "reasoning_episode", None) is None


def test_floor_reroute_records_only_the_replayed_episode(monkeypatch):
    ctx, deltas, records = _ctx("auto")
    s = _state(ctx)
    replies = [_completion(i, reasoning=f"шаг {i}") for i in range(1, 4)]
    replies += [
        _completion(4, reasoning="слабая", tool=False),
        _completion(5, reasoning="сильная"),
    ]
    sent = _patch_transport(monkeypatch, replies)
    for _ in range(3):
        _ask(s, ctx)
    del records[:]
    del deltas[:]
    _ask(s, ctx)
    assert sent[3:] == ["fast", "mid"]
    weak, strong = _resets(deltas)
    assert weak != strong
    assert records == [
        ReasoningRecord(
            text="сильная",
            duration_sec=THINK_SEC,
            attempt=strong,
        )
    ]


def test_flag_off_records_nothing_and_reset_stays_empty(monkeypatch):
    ctx, deltas, records = _ctx("default", record_reasoning=False)
    s = _state(ctx)
    _patch_transport(monkeypatch, [_completion(1, reasoning="план")])
    _ask(s, ctx)
    assert records == []
    assert _resets(deltas) == [""]


def test_flag_on_without_hook_records_nothing(monkeypatch):
    ctx, deltas, _ = _ctx("default", hook=False)
    s = _state(ctx)
    _patch_transport(monkeypatch, [_completion(1, reasoning="план")])
    _ask(s, ctx)
    assert _resets(deltas) == [""]
    assert getattr(ctx, "reasoning_episode", None) is None


def test_history_keeps_reasoning_out_under_strip_think(monkeypatch):
    ctx, _, records = _ctx("default", strip_think=True)
    s = _state(ctx)
    _patch_transport(monkeypatch, [_completion(1, reasoning="план")])
    resp_messages, _ = _ask(s, ctx)
    assert [r.text for r in records] == ["план"]
    assert "reasoning_content" not in json.dumps(resp_messages)
    assert "reasoning_content" not in json.dumps(s.messages)


# --- исполнитель: запись в тред ---------------------------------------------


async def _reasoning_run(tmp_path, monkeypatch):
    """Настоящий ран исполнителя: шаг с мыслью, затем финал без неё.
    Возвращает потоки треда ``messages`` и ``reasoning``."""
    from gc_core.agent_executor import AgentExecutor
    from gc_core.threads.file_store import FileThreadStore
    from gc_core.threads.models import ThreadKey
    from gc_core.utils.logging_module import release_run_logger
    from parity_harness import (
        ScriptedBackend,
        assistant_tool_call,
        install,
        parity_config,
    )

    work = assistant_tool_call("exec_calculator", {"expression": "3 * 4"})
    work["choices"][0]["message"]["reasoning_content"] = "посчитаю"
    finish = assistant_tool_call(
        "turn_finish",
        {
            "solution_type": "full_solution",
            "task": "Посчитать",
            "task_report": "Посчитано",
            "handoff": "{}",
        },
        "finish",
    )
    cfg = parity_config(
        tmp_path,
        monkeypatch,
        **{
            "agent.max_steps": 5,
            "agent.strict_turn_finish_validation": False,
            "text_answer_mode": "deny",
            "intent_scope.tools": {},
        },
    ).with_runtime(record_reasoning=True)
    monkeypatch.chdir(tmp_path)
    backend = install(monkeypatch, ScriptedBackend([work, finish]))
    ctx = await rc.acreate_run_context(cfg)
    try:
        await AgentExecutor(ctx).arun(
            ["Посчитай"],
            thread_id="thoughts",
            system="cli",
            start_new_thread=True,
            message_ids=["input-0"],
        )
    finally:
        release_run_logger(ctx.logger)
    backend.assert_clean()
    store = FileThreadStore(str(tmp_path / "threads"))
    key = ThreadKey(system="cli", thread="thoughts")
    return [
        [e.data for e in await store.read_stream(key, name)]
        for name in ("messages", "reasoning")
    ]


async def test_executor_writes_thought_before_its_step(tmp_path, monkeypatch):
    messages, (entry,) = await _reasoning_run(tmp_path, monkeypatch)
    expected = ReasoningRecord(
        text="посчитаю", duration_sec=None, attempt=None
    )
    assert entry["thought"] == expected.to_payload()
    user, *step = messages
    assert user["role"] == "user" and entry["after"] == user["id"]
    calls = [m["name"] for m in step if m.get("type") == "function_call"]
    assert calls == ["exec_calculator", "turn_finish"]
