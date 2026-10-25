"""Лестница пар в цикле агента (спека pair-ladder §1–§2): реальные RunContext и Router,
транспорт и бэкенд судьи замещены. Помощники — по образцу `test_pair_judge_wiring.py`:
тестовые файлы друг друга не импортируют."""

import json

from gc_core import processing_responses as pr
from gc_core import run_context as rc
from gc_core.auto_routing import judge_runner, state_ops
from gc_core.auto_routing.policy import PairState
from gc_core.config_modules.root import AppConfig
from gc_core.intent_log import append_user_ask
from gc_core.states import State
from gc_core.turn_boundary import begin_turn
from gc_testkit import run_coro

THREE = {
    "models": {"fast": {}, "mid": {}, "big": {}},
    "slots": {"light": "fast", "default": "mid", "max": "big"},
}
STEPS = {"weak_steps": 10, "strong_steps": 2, "pair_threshold": 4}


def _score(n):
    return json.dumps({"score": n, "reason": "r"})


def _ctx(level_box, registry=THREE):
    raw = {
        "backend": {"api_mode": "chat_completions"},
        "level": "auto",
        "logging": {"save_logs": False},
        **registry,
        "agent": {
            "auto_routing_safe_level": "auto",
            "auto_routing_two_level": STEPS,
        },
    }
    cfg = AppConfig.model_validate(raw)
    return run_coro(
        rc.acreate_run_context(cfg, choice_provider=lambda: level_box["value"])
    )


def _state(ctx):
    s = State(
        verbose=False,
        model="m",
        tools_spec=ctx.tools_spec,
        tools_descr=ctx.tools_description,
        skills_list=[],
        max_steps=80,
        system_prompt_id="v1",
        use_server_subagents=False,
    )
    s.backend_api_mode = "chat_completions"
    s.messages = [{"role": "system", "content": "SYS", "id": "m_sys"}]
    s.current_user_intent = append_user_ask(
        [], "fix the login page", user_turn_seq=0
    )
    ctx.attach_state(s)
    begin_turn(s, ctx)
    return s


def _read(call_id):
    return {
        "type": "function_call",
        "id": f"fc_{call_id}",
        "status": "completed",
        "call_id": call_id,
        "name": "fs_read_file",
        "arguments": json.dumps({"path": f"src/{call_id}.py"}),
    }


def _stub_transport(monkeypatch, n=40):
    calls = []
    responses = [([_read(f"c{i}")], f"r{i}", {}) for i in range(n)]

    async def fake(messages, state, ctx, *, resolved, **kwargs):
        calls.append(resolved.alias)
        return responses.pop(0)

    monkeypatch.setattr(pr, "_arequest_backend_canonical", fake)
    return calls


def _stub_judge(monkeypatch, reply):
    async def fake(
        *,
        ctx,
        state,
        messages,
        max_tokens,
        request_label,
        consumer,
        payload_overrides=None,
    ):
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(
        judge_runner, "arequest_auxiliary_completion_text", fake
    )


def _events(monkeypatch, ctx):
    events = []
    monkeypatch.setattr(
        ctx.logger,
        "info",
        lambda msg, *a, **k: events.append(k.get("extra", {})),
    )
    return events


def _pairs(events):
    return [e for e in events if e.get("event") == "routing_pair"]


def _ask(s, ctx, times=1):
    for _ in range(times):
        run_coro(pr.aprocess_user_ask([], s, ctx))


def test_a_score_of_four_starts_the_upper_pair_without_a_window(monkeypatch):
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    _stub_judge(monkeypatch, _score(4))
    events = _events(monkeypatch, ctx)
    _stub_transport(monkeypatch)
    _ask(s, ctx)
    assert [
        (e["cause"], e["to"], e["window_left"]) for e in _pairs(events)
    ] == [("start", "max", None)]
    assert _pairs(events)[0]["score"] == 4


def test_scores_not_below_the_threshold_and_a_failed_judge_keep_the_upper_pair_all_turn(
    monkeypatch,
):
    for reply in (_score(4), _score(5), RuntimeError("backend down")):
        ctx = _ctx({"value": "auto"})
        s = _state(ctx)
        _stub_judge(monkeypatch, reply)
        calls = _stub_transport(monkeypatch)
        _ask(s, ctx, times=14)
        assert "fast" not in calls and state_ops.read_pair(s) == PairState(
            "max", None, 0
        )


def test_a_score_below_the_threshold_starts_on_the_lower_pair(monkeypatch):
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    _stub_judge(monkeypatch, _score(3))
    calls = _stub_transport(monkeypatch)
    _ask(s, ctx, times=3)
    assert calls == ["mid", "mid", "fast"]


def test_a_signal_after_the_strong_lower_step_raises_then_the_second_locks(
    monkeypatch,
):
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    _stub_judge(monkeypatch, _score(2))
    events = _events(monkeypatch, ctx)
    calls = _stub_transport(monkeypatch)
    _ask(s, ctx, times=2)  # два шага на default нижней пары
    s.tick_signals = {"tool_call_error"}
    _ask(s, ctx)
    assert calls[-1] == "big" and s.last_routing_reason == "pair_raise"
    assert state_ops.read_pair(s) == PairState("max", 9, 1)
    s.pair_window_left = 0  # окно истекло
    _ask(s, ctx)
    assert calls[-1] == "mid" and s.last_routing_reason == "pair_descend"
    s.tick_signals = {"loop"}
    _ask(s, ctx)
    assert calls[-1] == "big" and state_ops.read_pair(s) == PairState(
        "max", None, 2
    )
    assert [e["cause"] for e in _pairs(events)] == [
        "start",
        "raise",
        "descend",
        "lock",
    ]
    assert _pairs(events)[1]["signals"] == ["tool_call_error"]


def test_a_signal_after_a_light_step_with_three_models_leaves_the_pair(
    monkeypatch,
):
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    _stub_judge(monkeypatch, _score(2))
    events = _events(monkeypatch, ctx)
    calls = _stub_transport(monkeypatch)
    _ask(s, ctx, times=3)  # mid, mid, fast
    s.tick_signals = {"loop"}
    _ask(s, ctx)
    assert calls[-1] == "mid" and s.turn_safe_level == "default"
    assert [e["cause"] for e in _pairs(events)] == ["start"]


def test_manual_steps_do_not_spend_the_raise_window(monkeypatch):
    box = {"value": "auto"}
    ctx = _ctx(box)
    s = _state(ctx)
    _stub_judge(monkeypatch, _score(2))
    _stub_transport(monkeypatch)
    _ask(s, ctx, times=2)  # два шага на default нижней пары
    s.tick_signals = {"loop"}
    _ask(s, ctx)  # подъём на weak_steps
    assert s.pair_window_left == 9
    s.tick_signals = set()
    box["value"] = "default"
    _ask(s, ctx, times=3)
    assert s.pair_window_left == 9
    box["value"] = "auto"
    _ask(s, ctx)
    assert s.pair_window_left == 8


def test_a_retried_tick_does_not_raise_the_pair_twice(monkeypatch):
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    _stub_judge(monkeypatch, _score(2))
    _stub_transport(monkeypatch)
    _ask(s, ctx, times=2)
    s.tick_signals = {"loop"}
    run_coro(pr.aroute_before_call(s, ctx))
    run_coro(pr.aroute_before_call(s, ctx))  # ретрай того же такта — мемо
    s._tick_decision = None
    run_coro(pr.aroute_before_call(s, ctx))  # мемо сбито — пара уже верхняя
    assert s.pair_raises == 1


def test_a_dead_alias_does_not_break_the_ladder(monkeypatch):
    """Один разомкнутый предохранитель: мёртвый уровень заменяется живым, как без
    лестницы, — признак «сильная нижней пары» не спрашивает предохранитель."""
    for score, expected in (
        (2, ["big", "big", "fast", "fast"]),
        (5, ["big"] * 4),
    ):
        ctx = _ctx({"value": "auto"})
        s = _state(ctx)
        ctx.model_router.breaker.trip("mid", "5xx после ретраев")
        _stub_judge(monkeypatch, _score(score))
        calls = _stub_transport(monkeypatch)
        _ask(s, ctx, times=4)
        assert calls == expected, score
