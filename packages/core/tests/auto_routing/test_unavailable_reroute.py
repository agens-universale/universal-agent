"""Замена подтверждённо недоступной модели в AUTO (спека PR140 Р3): тот же такт
повторяется на ближайшем живом уровне (сначала вверх), без второго судьи и без
перечитывания /level; ручной режим и «все мертвы» — честная ошибка.

Транспорт замещён на одну попытку (`_arequest_backend_canonical_once`): настоящая
обёртка сама переводит 503 в честную ошибку и взводит предохранитель."""
import json

import httpx
import pytest

from gc_core import processing_responses as pr
from gc_core import run_context as rc
from gc_core.auto_routing.pair_judge import SOURCE_JUDGE, PairChoice
from gc_core.config_modules.root import AppConfig
from gc_core.execution.contracts import MainModelUnavailable
from gc_core.intent_guard.guard import FinishGateDecision
from gc_core.states import State
from gc_core.turn_boundary import begin_turn
from gc_testkit import as_coro, run_coro

RAW = {
    "models": {"fast": {}, "mid": {}, "big": {}},
    "slots": {"light": "fast", "default": "mid", "max": "big"},
    "backend": {"api_mode": "chat_completions"},
    "level": "auto",
    "logging": {"save_logs": False},
}
STEPS = {"weak_steps": 10, "strong_steps": 2}
FINISH = json.dumps({"solution_type": "full_solution", "task": "t",
                     "task_report": "Готово.", "handoff": "{}"})


def _ctx(box, registry=None, **agent):
    raw = dict(RAW)
    if registry is not None:
        raw.update(registry)
    raw["agent"] = {"auto_routing_safe_level": "default",
                    "auto_routing_two_level": STEPS, **agent}
    cfg = AppConfig.model_validate(raw)
    return run_coro(rc.acreate_run_context(cfg, level_provider=lambda: box["value"]))


def _state(ctx):
    s = State(verbose=False, model="m", tools_spec=ctx.tools_spec, tools_descr=ctx.tools_description,
              skills_list=[], max_steps=50, system_prompt_id="v1", use_server_subagents=False)
    s.backend_api_mode = "chat_completions"
    s.messages = [{"role": "system", "content": "SYS", "id": "m_sys"}]
    ctx.attach_state(s)
    begin_turn(s, ctx)
    return s


def _fc(name, call_id, arguments):
    return {"type": "function_call", "id": f"fc_{call_id}", "status": "completed",
            "call_id": call_id, "name": name, "arguments": arguments}


def _read(call_id):
    return _fc("fs_read_file", call_id, json.dumps({"path": f"src/{call_id}.py"}))


def _write(call_id):
    return _fc("fs_write_file", call_id, json.dumps({"path": "src/a.py", "content": "x = 1\n"}))


def _unavailable():
    request = httpx.Request("POST", "https://synthetic.invalid")
    return httpx.HTTPStatusError("provider unavailable", request=request,
                                 response=httpx.Response(503, request=request))


def _transport(monkeypatch, dead=(), reply=None, on_dead=None):
    """Одна попытка транспорта: алиасы из `dead` отвечают 503, остальные —
    `reply(alias, n)` (по умолчанию одно чтение). Возвращает журнал алиасов."""
    sent = []

    async def once(messages, state, ctx, *, resolved, set_previous_response_id=True, mute_answer=False):
        sent.append(resolved.alias)
        if resolved.alias in dead:
            if on_dead is not None:
                on_dead()
            raise _unavailable()
        n = len(sent)
        calls = reply(resolved.alias, n) if reply else [_read(f"c{n}")]
        return calls, f"r{n}", {}

    monkeypatch.setattr(pr, "_arequest_backend_canonical_once", once)
    return sent


def _ask(s, ctx, times=1):
    result = None
    for _ in range(times):
        result = run_coro(pr.aprocess_user_ask([], s, ctx))
    return result


def test_a_dead_weak_model_mid_turn_is_replaced_by_the_nearest_live_level(monkeypatch):
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    sent = _transport(monkeypatch, dead={"fast"})
    _ask(s, ctx, times=3)
    assert sent == ["mid", "mid", "fast", "mid"]
    assert s._call_level == "default" and s.current_level == "default"
    assert ctx.model_router.breaker.is_open("fast")
    assert s.steps_in_turn == 3
    _ask(s, ctx)
    assert sent[4:] == ["mid"]          # мёртвый алиас больше не пробуется


def test_a_dead_strong_model_of_the_lower_pair_is_replaced_by_max(monkeypatch):
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    sent = _transport(monkeypatch, dead={"mid"})
    _ask(s, ctx)
    assert sent == ["mid", "big"]
    assert s._call_level == "max"
    # Причина предохранителя — исходная, а не текст честной ошибки: иначе
    # следующая честная ошибка по этому алиасу вложила бы в себя прежнюю.
    assert ctx.model_router.breaker.snapshot() == {"mid": "provider unavailable"}


def test_when_every_level_is_dead_the_turn_fails_with_the_honest_error(monkeypatch):
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    sent = _transport(monkeypatch, dead={"fast", "mid", "big"})
    with pytest.raises(MainModelUnavailable):
        _ask(s, ctx)
    assert sent == ["mid", "big", "fast"]
    assert s.steps_in_turn == 0


def test_manual_level_keeps_the_honest_error(monkeypatch):
    ctx = _ctx({"value": "default"})
    s = _state(ctx)
    sent = _transport(monkeypatch, dead={"mid"})
    with pytest.raises(MainModelUnavailable):
        _ask(s, ctx)
    assert sent == ["mid"]


def test_the_pair_judge_is_not_asked_again_after_a_replacement(monkeypatch):
    judge_calls = []

    async def pair(*args, **kwargs):
        judge_calls.append(True)
        return PairChoice("default", 2, "small", SOURCE_JUDGE, None)

    monkeypatch.setattr(pr, "arun_pair_judge", pair)
    ctx = _ctx({"value": "auto"}, auto_routing_safe_level="auto")
    s = _state(ctx)
    sent = _transport(monkeypatch, dead={"mid"})
    _ask(s, ctx)
    assert judge_calls == [True]
    assert sent == ["mid", "big"]


def test_a_level_switch_during_the_failed_request_waits_for_the_next_tick(monkeypatch):
    box = {"value": "auto"}
    ctx = _ctx(box)
    s = _state(ctx)
    sent = _transport(monkeypatch, dead={"fast"}, on_dead=lambda: box.update(value="max"))
    _ask(s, ctx, times=3)
    assert sent == ["mid", "mid", "fast", "mid"]
    _ask(s, ctx)
    assert sent[-1] == "big"            # ручной max — со следующего такта


def test_a_dead_replay_target_moves_the_replay_up(monkeypatch):
    """Слабая сделала важный вызов, сильная пары умерла прямо на переигрыше —
    переигрыш идёт следующим живым кандидатом пола (`floor_levels`)."""
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    dead = set()
    reply = lambda alias, n: [_write(f"c{n}")] if alias in ("fast", "big") else [_read(f"c{n}")]
    sent = _transport(monkeypatch, dead=dead, reply=reply)
    _ask(s, ctx, times=2)                    # две сильные: mid, mid
    dead.add("mid")                          # сильная умирает к третьему шагу
    _ask(s, ctx)                             # fast пишет → пол на mid (мёртв) → big
    assert sent == ["mid", "mid", "fast", "mid", "big"]
    assert s._call_level == "max"


def test_a_copy_of_the_dead_model_is_tried_and_skipped_without_looping(monkeypatch):
    entry = {"model": "mid-model", "url": "http://mid/v1/chat/completions", "token": ""}
    ctx = _ctx({"value": "auto"}, registry={
        "models": {"fast": {}, "mid": entry, "copy": entry},
        "slots": {"light": "fast", "default": "mid", "max": "copy"}})
    s = _state(ctx)
    sent = _transport(monkeypatch, dead={"mid", "copy"})
    _ask(s, ctx)
    assert sent == ["mid", "copy", "fast"]


def test_a_failed_attempt_leaves_no_trace_in_the_history(monkeypatch):
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    _transport(monkeypatch, dead={"mid"})
    before = list(s.messages)
    response, _ = _ask(s, ctx)
    assert s.messages == before
    assert response == [_read("c2")]


def test_old_durable_histories_keep_the_honest_error(monkeypatch):
    ctx = _ctx({"value": "auto"})
    ctx.execution_patch = lambda patch: patch != pr.UNAVAILABLE_REROUTE_PATCH
    s = _state(ctx)
    sent = _transport(monkeypatch, dead={"mid"})
    with pytest.raises(MainModelUnavailable):
        _ask(s, ctx)
    assert sent == ["mid"]


class _AcceptingGate:
    enabled = False

    def __init__(self, **_kwargs):
        pass

    async def aevaluate_turn_finish(self, *, finish_handoff_arguments):
        return FinishGateDecision(outcome="accept")


def test_tools_of_the_turn_run_once_across_a_replacement(monkeypatch):
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)

    def reply(alias, n):
        if n == 4:
            return [_write("w")]
        if n >= 5:
            return [_fc("turn_finish", "fin", FINISH)]
        return [_read(f"c{n}")]

    sent = _transport(monkeypatch, dead={"fast"}, reply=reply)
    executed = []
    monkeypatch.setattr(pr, "aapply_tool", as_coro(lambda **kw: executed.append(kw["call_id"]) or '{"ok": true}'))
    monkeypatch.setattr(pr, "FinishGateGuard", _AcceptingGate)
    monkeypatch.setattr(pr, "amaybe_update_intent_scope_from_tool", as_coro(lambda **kw: None))
    run_coro(pr.arun_agent([{"role": "user", "content": "сделай", "id": "u1"}], s, ctx))
    assert sent[:4] == ["mid", "mid", "fast", "mid"]
    assert executed.count("w") == 1 and executed.count("c1") == 1 and executed.count("c2") == 1
