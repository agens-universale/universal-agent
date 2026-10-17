"""Судья пары в цикле агента (спека pair-judge §1–§2): реальные RunContext и Router,
транспорт и бэкенд судьи замещены. Помощники — по образцу `test_two_level_wiring.py`:
тестовые файлы друг друга не импортируют."""

import json

from gc_core import processing_responses as pr
from gc_core import run_context as rc
from gc_core.auto_routing import judge_runner
from gc_core.auto_routing.policy import RoutingParams
from gc_core.config_modules.root import AppConfig
from gc_core.intent_log import append_user_ask
from gc_core.states import State
from gc_core.turn_boundary import begin_turn
from gc_testkit import run_coro

RAW = {
    "models": {"fast": {}, "mid": {}, "big": {}},
    "slots": {"light": "fast", "default": "mid", "max": "big"},
    "backend": {"api_mode": "chat_completions"},
    "level": "auto",
    "logging": {"save_logs": False},
}
#: Три шага на сильной — запас места между стартом хода и спуском, как в соседнем файле.
STEPS = {"weak_steps": 10, "strong_steps": 3, "pair_threshold": 4}
EASY = '{"score": 2, "reason": "one file"}'
HARD = '{"score": 5, "reason": "new subsystem"}'


def _ctx(level_box, *, registry=None, **agent):
    raw = dict(RAW)
    if registry is not None:
        raw = {k: v for k, v in raw.items() if k not in ("models", "slots")}
        raw.update(registry)
    raw["agent"] = {
        "auto_routing_safe_level": "auto",
        "auto_routing_two_level": STEPS,
        **agent,
    }
    cfg = AppConfig.model_validate(raw)
    return run_coro(
        rc.acreate_run_context(cfg, level_provider=lambda: level_box["value"])
    )


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


def _write(call_id):
    return {
        "type": "function_call",
        "id": f"fc_{call_id}",
        "status": "completed",
        "call_id": call_id,
        "name": "fs_write_file",
        "arguments": json.dumps({"path": "src/a.py", "content": "x = 1\n"}),
    }


def _reads(first, last):
    return [([_read(f"c{i}")], f"r{i}", {}) for i in range(first, last + 1)]


def _stub_transport(monkeypatch, responses):
    calls = []

    async def fake(messages, state, ctx, *, resolved, **kwargs):
        calls.append(resolved.alias)
        return responses.pop(0)

    monkeypatch.setattr(pr, "_arequest_backend_canonical", fake)
    return calls


def _stub_judge(monkeypatch, *replies):
    """Бэкенд судьи пары: ответы по порядку вызовов, последний повторяется."""
    calls = []

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
        calls.append(request_label)
        reply = replies[min(len(calls), len(replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(
        judge_runner, "arequest_auxiliary_completion_text", fake
    )
    return calls


def _events(monkeypatch, ctx):
    events = []
    monkeypatch.setattr(
        ctx.logger,
        "info",
        lambda msg, *a, **k: events.append(k.get("extra", {})),
    )
    return events


def _pair_events(events):
    return [e for e in events if e.get("event") == "routing_pair_judge"]


def _ask(s, ctx, times=1):
    for _ in range(times):
        run_coro(pr.aprocess_user_ask([], s, ctx))


def test_an_easy_score_runs_the_turn_on_the_light_default_pair(monkeypatch):
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    judge = _stub_judge(monkeypatch, EASY)
    calls = _stub_transport(monkeypatch, _reads(1, 4))
    _ask(s, ctx, times=4)
    assert judge == ["routing_judge_pair"]
    assert calls == ["mid", "mid", "mid", "fast"]
    assert s.turn_safe_level == "default"


def test_a_hard_score_runs_the_turn_on_the_default_max_pair(monkeypatch):
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    judge = _stub_judge(monkeypatch, HARD)
    calls = _stub_transport(monkeypatch, _reads(1, 4))
    _ask(s, ctx, times=4)
    assert len(judge) == 1 and calls == ["big", "big", "big", "mid"]


def test_a_fixed_safe_level_never_calls_the_judge(monkeypatch):
    ctx = _ctx({"value": "auto"}, auto_routing_safe_level="default")
    s = _state(ctx)
    judge = _stub_judge(monkeypatch, HARD)
    _stub_transport(monkeypatch, _reads(1, 2))
    _ask(s, ctx, times=2)
    assert judge == []


def test_one_model_everywhere_never_calls_the_judge(monkeypatch):
    ctx = _ctx(
        {"value": "auto"},
        registry={"models": {"only": {}}, "slots": {"default": "only"}},
    )
    s = _state(ctx)
    judge = _stub_judge(monkeypatch, HARD)
    _stub_transport(monkeypatch, _reads(1, 2))
    _ask(s, ctx, times=2)
    assert judge == []


def test_two_models_run_the_light_default_pair_without_the_judge(monkeypatch):
    """С двумя моделями лестница вырождается (сложная пара — одна default) — дороже ss2 при
    том же качестве (замер pair-judge): `auto` работает обычной парой, судью не зовёт."""
    ctx = _ctx(
        {"value": "auto"},
        registry={
            "models": {"fast": {}, "mid": {}},
            "slots": {"light": "fast", "default": "mid"},
        },
    )
    s = _state(ctx)
    judge = _stub_judge(monkeypatch, HARD)
    calls = _stub_transport(monkeypatch, _reads(1, 5))
    _ask(s, ctx, times=5)
    assert judge == [] and ctx.routing_params.safe_level == "default"
    assert calls == ["mid"] * 3 + ["fast"] * 2


def test_layout_b_runs_the_default_max_pair_without_the_judge(monkeypatch):
    """light и default — одна модель: нижняя пара вырождена, обычная пара — default+max."""
    ctx = _ctx(
        {"value": "auto"},
        registry={
            "models": {"fast": {}, "big": {}},
            "slots": {"light": "fast", "default": "fast", "max": "big"},
        },
    )
    s = _state(ctx)
    judge = _stub_judge(monkeypatch, EASY)
    calls = _stub_transport(monkeypatch, _reads(1, 5))
    _ask(s, ctx, times=5)
    assert judge == [] and ctx.routing_params.safe_level == "max"
    assert calls == ["big"] * 3 + ["fast"] * 2


def test_switching_to_auto_mid_turn_calls_the_judge_once(monkeypatch):
    box = {"value": "default"}
    ctx = _ctx(box)
    s = _state(ctx)
    judge = _stub_judge(monkeypatch, EASY)
    events = _events(monkeypatch, ctx)
    _stub_transport(monkeypatch, _reads(1, 5))
    _ask(s, ctx, times=2)  # ручной режим — судьи нет
    assert judge == []
    box["value"] = "auto"
    _ask(s, ctx)
    assert len(judge) == 1 and _pair_events(events)[0]["mid_turn"] is True
    box["value"] = "default"
    _ask(s, ctx)
    box["value"] = "auto"
    _ask(s, ctx)  # auto → ручной → auto: второго вызова нет
    assert len(judge) == 1 and s.turn_safe_level == "default"


def test_a_new_turn_calls_the_judge_again(monkeypatch):
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    judge = _stub_judge(monkeypatch, EASY, HARD)
    calls = _stub_transport(monkeypatch, _reads(1, 2))
    _ask(s, ctx)
    begin_turn(s, ctx)
    _ask(s, ctx)
    assert len(judge) == 2 and calls == ["mid", "big"]


def test_a_failed_judge_runs_the_turn_on_the_hard_pair(monkeypatch):
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    _stub_judge(monkeypatch, RuntimeError("backend down"))
    events = _events(monkeypatch, ctx)
    calls = _stub_transport(monkeypatch, _reads(1, 1))
    _ask(s, ctx)
    assert calls == ["big"]
    (ev,) = _pair_events(events)
    assert ev["source"] == "fallback" and ev["error"].startswith(
        "RuntimeError"
    )


def test_a_retried_tick_does_not_call_the_judge_twice(monkeypatch):
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    judge = _stub_judge(monkeypatch, EASY)
    run_coro(pr.aroute_before_call(s, ctx))
    run_coro(
        pr.aroute_before_call(s, ctx)
    )  # ретрай того же такта (close_tick не звали)
    assert len(judge) == 1


def test_a_weak_write_on_the_easy_pair_is_replayed_on_default(monkeypatch):
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    _stub_judge(monkeypatch, EASY)
    events = _events(monkeypatch, ctx)
    calls = _stub_transport(
        monkeypatch,
        _reads(1, 3)
        + [([_write("w1")], "w1", {}), ([_write("w2")], "w2", {})],
    )
    _ask(s, ctx, times=4)
    assert calls == ["mid", "mid", "mid", "fast", "mid"]
    (ev,) = [e for e in events if e.get("event") == "artifact_reroute"]
    assert ev["from_level"] == "light" and ev["to_level"] == "default"


def test_a_turn_started_in_auto_is_not_mid_turn(monkeypatch):
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    _stub_judge(monkeypatch, EASY)
    events = _events(monkeypatch, ctx)
    _stub_transport(monkeypatch, _reads(1, 1))
    _ask(s, ctx)
    assert _pair_events(events)[0]["mid_turn"] is False


def test_a_chosen_pair_is_not_judged_again_even_without_the_tick_memo(
    monkeypatch,
):
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    judge = _stub_judge(monkeypatch, EASY)
    run_coro(pr.aroute_before_call(s, ctx))
    s._tick_decision = None  # мемо обесценено (как при смене /level)
    run_coro(pr.aroute_before_call(s, ctx))
    assert len(judge) == 1 and s.turn_safe_level == "default"
