"""Политика `two_level` в цикле агента (спека two_level §3–§6): реальные RunContext
и Router, транспорт замещён.

Тестовые файлы друг друга не импортируют. Заглушка транспорта всегда отдаёт НЕПУСТОЙ ответ реальной
формы `function_call`: пустой ответ под `two_level` — повод `no_tool_call`, и
каждый вызов слабой модели переигрывался бы. Аргументы вызовов проходят настоящую
схему тула: под `two_level` ответ слабой модели сверяется с реестром такта."""
import json
from pathlib import Path

import pytest

from gc_core import processing_responses as pr
from gc_core import run_context as rc
from gc_core.auto_routing.policy import RoutingParams
from gc_core.config_modules.root import AppConfig
from gc_core.settings import override_settings
from gc_core.intent_guard.guard import FinishGateDecision
from gc_core.states import State
from gc_core.turn_boundary import begin_turn
from gc_core.usage.account import CATEGORY_MAIN as USAGE_CATEGORY_MAIN
from gc_testkit import as_coro, run_coro

RAW = {
    "models": {"fast": {}, "mid": {}, "big": {}},
    "slots": {"light": "fast", "default": "mid", "max": "big"},
    "backend": {"api_mode": "chat_completions"},
    "level": "auto",
    "logging": {"save_logs": False},
}

FINISH_ARGUMENTS = json.dumps({
    "solution_type": "full_solution", "task": "поправить тест",
    "task_report": "Тест исправлен, набор зелёный.", "handoff": "{}",
})


#: Числа политики закреплены: файл проверяет механику удержания и переигрыша, а не
#: умолчание (его проверяют `test_two_level_policy` и `test_auto_routing_config_surface`).
#: Три шага на сильной дают сценариям запас места между стартом хода и спуском.
STEPS = {"weak_steps": 10, "strong_steps": 3}


def _ctx(level_box, *, registry=None, **agent):
    """Реальный RunContext под `two_level`. `agent` — ключи секции `agent`
    (в т.ч. свои числа `auto_routing_two_level`), `registry` —
    замена секций `models`/`slots`."""
    raw = dict(RAW)
    if registry is not None:
        raw = {k: v for k, v in raw.items() if k not in ("models", "slots")}
        raw.update(registry)
    # Фиксированная пара light+default: этот набор — про политику two_level, а не про лестницу
    # пар (умолчание safe_level — auto, с тремя моделями оно включило бы судью пары).
    raw["agent"] = {"auto_routing_safe_level": "default",
                    "auto_routing_two_level": STEPS, **agent}
    cfg = AppConfig.model_validate(raw)
    return run_coro(rc.acreate_run_context(cfg, level_provider=lambda: level_box["value"]))


def _state(ctx):
    s = State(verbose=False, model="m", tools_spec=ctx.tools_spec, tools_descr=ctx.tools_description,
              skills_list=[], max_steps=50, system_prompt_id="v1", use_server_subagents=False)
    s.backend_api_mode = "chat_completions"
    s.messages = [{"role": "system", "content": "SYS", "id": "m_sys"}]
    ctx.attach_state(s)
    begin_turn(s, ctx)
    return s


def _fc(name, call_id, arguments):
    """Вызов тула в канонической форме ответа (как отдаёт
    `output_chat_completion_to_response`)."""
    return {"type": "function_call", "id": f"fc_{call_id}", "status": "completed",
            "call_id": call_id, "name": name, "arguments": arguments}


def _read(call_id):
    """Неважный вызов с аргументами по схеме; путь свой у каждого вызова."""
    return _fc("fs_read_file", call_id, json.dumps({"path": f"src/{call_id}.py"}))


def _write(call_id):
    return _fc("fs_write_file", call_id, json.dumps({"path": "src/a.py", "content": "x = 1\n"}))


def _prose(text):
    return {"type": "message", "id": "msg_1", "status": "completed", "role": "assistant",
            "content": [{"type": "output_text", "annotations": [], "logprobs": [], "text": text}]}


def _reads(first, last):
    """Ответы с одним неважным вызовом: по одному на вызов транспорта."""
    return [([_read(f"c{i}")], f"r{i}", {}) for i in range(first, last + 1)]


def _stub_transport(monkeypatch, responses, spend=None):
    """`responses` — `(resp_messages, response_id, res_headers)` по одному на КАЖДЫЙ
    вызов транспорта, включая переигранный. `spend` — `(input, output, cost)` на
    каждый вызов: нужен там, где проверяется, чей расход попал в событие.
    Возвращает список снимков вызовов: алиас модели, схемы тулов, системный промпт,
    приглушён ли канал `answer`."""
    calls = []

    async def fake(messages, state, ctx, *, resolved, **kwargs):
        if spend is not None:
            inp, out, cost = spend[len(calls)]
            state.usage.add(USAGE_CATEGORY_MAIN, inp, out, cost=cost, billed=True, currency="USD")
        calls.append({"alias": resolved.alias, "tools": json.dumps(state.tools),
                      "tool_names": {tool["name"] for tool in state.tools},
                      "system": state.messages[0]["content"],
                      "muted": bool(kwargs.get("mute_answer", False))})
        return responses.pop(0)

    monkeypatch.setattr(pr, "_arequest_backend_canonical", fake)
    return calls


def _aliases(calls):
    return [call["alias"] for call in calls]


def _capture_events(monkeypatch, ctx):
    events = []
    monkeypatch.setattr(ctx.logger, "info", lambda msg, *a, **k: events.append(k.get("extra", {})))
    return events


def _reroutes(events):
    return [e for e in events if e.get("event") == "artifact_reroute"]


def _ask(s, ctx, times=1):
    for _ in range(times):
        result = run_coro(pr.aprocess_user_ask([], s, ctx))
    return result


# --- решение такта --------------------------------------------------------------

def test_the_turn_starts_on_the_strong_level_and_descends_after_strong_steps(monkeypatch):
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    calls = _stub_transport(monkeypatch, _reads(1, 5))
    reasons = []
    for _ in range(5):
        _ask(s, ctx)
        reasons.append(s.last_routing_reason)
    assert _aliases(calls) == ["mid", "mid", "mid", "fast", "fast"]
    assert reasons == ["start_turn", "hold", "hold", "descend", "hold"]
    assert s.current_level == "light" and s.steps_on_current_level == 2


def test_safe_level_max_moves_the_pair_to_default_and_max(monkeypatch):
    ctx = _ctx({"value": "auto"}, auto_routing_safe_level="max")
    s = _state(ctx)
    calls = _stub_transport(monkeypatch, _reads(1, 4))
    _ask(s, ctx, times=4)
    assert _aliases(calls) == ["big", "big", "big", "mid"]


def test_the_numbers_come_from_the_config_and_weak_steps_in_a_row_promote(monkeypatch):
    ctx = _ctx({"value": "auto"}, auto_routing_two_level={"weak_steps": 2, "strong_steps": 1})
    s = _state(ctx)
    calls = _stub_transport(monkeypatch, _reads(1, 5))
    reasons = []
    for _ in range(5):
        _ask(s, ctx)
        reasons.append(s.last_routing_reason)
    assert _aliases(calls) == ["mid", "fast", "fast", "mid", "fast"]
    assert reasons == ["start_turn", "descend", "hold", "promote", "descend"]


def test_the_routing_decision_event_carries_the_policy_and_the_descend_reason(monkeypatch):
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    _stub_transport(monkeypatch, _reads(1, 4))
    events = _capture_events(monkeypatch, ctx)
    _ask(s, ctx, times=4)
    decisions = [e for e in events if e.get("event") == "routing_decision"]
    assert [e["reason"] for e in decisions] == ["start_turn", "hold", "hold", "descend"]
    assert decisions[-1]["policy"] == "two_level" and decisions[-1]["routing_level"] == "light"
    assert decisions[-1]["heuristics"] == []


# --- сигналы --------------------------------------------------------------------

def test_a_signal_on_the_strong_level_restarts_its_series(monkeypatch):
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    calls = _stub_transport(monkeypatch, _reads(1, 6))
    events = _capture_events(monkeypatch, ctx)
    _ask(s, ctx, times=2)
    s.tick_signals = {"tool_call_error"}
    _ask(s, ctx)                                        # третий шаг сильной — с сигналом
    assert s.last_routing_reason == "heuristics" and s.steps_on_current_level == 1
    ev = [e for e in events if e.get("event") == "routing_decision"][-1]
    assert ev["heuristics"] == ["tool_call_error"]
    _ask(s, ctx, times=3)
    assert _aliases(calls) == ["mid", "mid", "mid", "mid", "mid", "fast"]


def test_a_signal_on_the_weak_level_raises_to_the_strong_at_once(monkeypatch):
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    calls = _stub_transport(monkeypatch, _reads(1, 5))
    _ask(s, ctx, times=4)
    assert s.current_level == "light"
    s.tick_signals = {"loop"}
    _ask(s, ctx)
    assert _aliases(calls)[-1] == "mid" and s.current_level == "default"
    assert s.last_routing_reason == "heuristics" and s.steps_on_current_level == 1


class _AcceptingGate:
    """Судья финала без LLM: финал принимается."""
    enabled = False

    def __init__(self, **_kwargs):
        pass

    async def aevaluate_turn_finish(self, *, finish_handoff_arguments):
        return FinishGateDecision(outcome="accept")


def test_arguments_past_the_schema_from_the_strong_model_restart_its_series(monkeypatch):
    """Сквозной путь сигнала `tool_call_error` (спека §5): сильная прислала вызов
    мимо схемы — он не переигрывается, батч его отбивает настоящей валидацией
    `aapply_tool`, и серия сильной начинается заново. Без сигнала третий вызов
    ушёл бы на слабую."""
    ctx = _ctx({"value": "auto"}, auto_routing_two_level={"weak_steps": 10, "strong_steps": 2})
    s = _state(ctx)
    bad = _fc("fs_read_file", "bad", json.dumps({"offset": 3}))          # нет обязательного path
    finish = [_fc("turn_finish", "fin", FINISH_ARGUMENTS)]
    responses = [([_read("c1")], "r1", {}), ([bad], "r2", {}), ([_read("c3")], "r3", {}),
                 ([_read("c4")], "r4", {}), ([_read("c5")], "r5", {}), (finish, "r6", {}),
                 (finish, "r7", {})]
    calls = _stub_transport(monkeypatch, responses)
    real_apply_tool = pr.aapply_tool

    async def aapply_tool(**kw):
        if kw["call_id"] == "bad":
            return await real_apply_tool(**kw)           # настоящая валидация аргументов
        return json.dumps({"tool": kw["tool_name"], "ok": True})

    monkeypatch.setattr(pr, "aapply_tool", aapply_tool)
    monkeypatch.setattr(pr, "FinishGateGuard", _AcceptingGate)
    monkeypatch.setattr(pr, "amaybe_update_intent_scope_from_tool", as_coro(lambda **_kw: None))

    run_coro(pr.arun_agent([{"role": "user", "content": "почини тест", "id": "u1"}], s, ctx))

    assert _aliases(calls) == ["mid", "mid", "mid", "mid", "fast", "fast", "mid"]


# --- переигрыш ------------------------------------------------------------------

def test_an_important_tool_from_the_weak_model_is_replayed_once_on_the_strong(monkeypatch):
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    replayed = [_write("c5")]
    responses = _reads(1, 3) + [([_write("c4")], "r4", {}), (replayed, "r5", {})] + _reads(6, 8)
    calls = _stub_transport(
        monkeypatch, responses,
        spend=[(10, 1, 0.0001)] * 3 + [(1200, 47, 0.0041), (1500, 90, 0.03)] + [(10, 1, 0.0001)] * 3,
    )
    events = _capture_events(monkeypatch, ctx)
    _ask(s, ctx, times=3)

    resp, rid = _ask(s, ctx)

    assert _aliases(calls) == ["mid", "mid", "mid", "fast", "mid"]
    assert resp == replayed and rid == "r5"              # выброшенный c4 наружу не вышел
    assert s.current_level == "default" and s.last_routing_reason == "important"
    assert s.steps_in_turn == 4 and s.steps_on_current_level == 1
    (ev,) = _reroutes(events)
    assert ev["triggers"] == ["important"] and ev["tools"] == ["fs_write_file"]
    assert ev["from_level"] == "light" and ev["to_level"] == "default"
    assert ev["prompt_tokens"] == 1200 and ev["completion_tokens"] == 47
    assert ev["cost"] == {"USD": pytest.approx(0.0041)}
    assert ev["discarded_calls"] == [{"name": "fs_write_file", "arguments": _write("c4")["arguments"]}]
    assert "discarded_text" not in ev                    # прозы в выброшенном ответе не было

    _ask(s, ctx, times=3)                                # переигранный шаг — первый из strong_steps
    assert _aliases(calls)[5:] == ["mid", "mid", "fast"]


def test_the_discarded_answer_survives_the_jsonl_formatter(monkeypatch, tmp_path):
    """Поля выброшенного ответа доходят до готовой строки лога через настоящий
    JsonlFormatter на per-run логгере — класс регресса «ключ `extra` молча потерян
    форматтером» (см. `test_routing_decision_survives_jsonl_formatter`)."""
    raw = {**RAW, "agent": {"auto_routing_safe_level": "default",
                            "auto_routing_two_level": STEPS},
           "logging": {"save_logs": True, "format": "jsonl", "console_trace": False}}
    cfg = AppConfig.model_validate(raw)
    cfg = override_settings(cfg, {"paths.logs_dump_dir": str(tmp_path)})
    ctx = run_coro(rc.acreate_run_context(cfg, level_provider=lambda: "auto"))
    s = _state(ctx)
    _stub_transport(monkeypatch, _reads(1, 3) + [([_write("c4")], "r4", {}), ([_write("c5")], "r5", {})])
    _ask(s, ctx, times=4)
    lines = Path(ctx.logging_path).read_text(encoding="utf-8").splitlines()
    (ev,) = [e for e in map(json.loads, filter(str.strip, lines)) if e.get("event") == "artifact_reroute"]
    assert ev["discarded_calls"] == [{"name": "fs_write_file", "arguments": _write("c4")["arguments"]}]


def test_the_finish_is_replayed_like_a_file_write(monkeypatch):
    call = _fc("turn_finish", "c4", FINISH_ARGUMENTS)
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    calls = _stub_transport(monkeypatch, _reads(1, 3) + [([call], "r4", {}), ([call], "r5", {})])
    events = _capture_events(monkeypatch, ctx)
    _ask(s, ctx, times=4)
    assert _aliases(calls) == ["mid", "mid", "mid", "fast", "mid"]
    assert _reroutes(events)[0]["tools"] == [call["name"]]


def test_a_plan_update_from_the_weak_model_is_kept_and_the_weak_model_goes_on(monkeypatch):
    """Отметка плана — не повод: ответ слабой идёт в дело, сильная не подхватывает
    ход, следующий шаг — снова на слабой (замер 2026-09-24: переигрыш плана давал
    половину переигрышей и до 44 % шагов сильной)."""
    call = _fc("session_todo", "c4", json.dumps({"todos": [{"content": "починить тест", "status": "completed"}]}))
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    calls = _stub_transport(monkeypatch, _reads(1, 3) + [([call], "r4", {})] + _reads(5, 5))
    events = _capture_events(monkeypatch, ctx)
    _ask(s, ctx, times=5)
    assert _aliases(calls) == ["mid", "mid", "mid", "fast", "fast"]
    assert _reroutes(events) == []


@pytest.mark.parametrize("call", [
    _fc("fs_delete_everything", "c4", json.dumps({"path": "src"})),
    _fc("fs_read_file", "c4", json.dumps({"offset": 3})),
], ids=["unknown_tool", "arguments_past_the_schema"])
def test_a_broken_call_from_the_weak_model_is_replayed(monkeypatch, call):
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    calls = _stub_transport(monkeypatch, _reads(1, 3) + [([call], "r4", {})] + _reads(5, 5))
    events = _capture_events(monkeypatch, ctx)
    _ask(s, ctx, times=3)
    resp, rid = _ask(s, ctx)
    assert _aliases(calls) == ["mid", "mid", "mid", "fast", "mid"] and rid == "r5"
    (ev,) = _reroutes(events)
    assert ev["triggers"] == ["invalid_call"] and ev["tools"] == []
    assert s.last_routing_reason == "invalid_call" and s.current_level == "default"


@pytest.mark.parametrize("answer", [[_prose("Готово: тест исправлен.")], []], ids=["prose", "empty"])
def test_an_answer_without_tool_calls_from_the_weak_model_is_replayed(monkeypatch, answer):
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    strong_prose = [_prose("Тест исправлен, набор зелёный.")]
    calls = _stub_transport(monkeypatch, _reads(1, 3) + [(answer, "r4", {}), (strong_prose, "r5", {})])
    events = _capture_events(monkeypatch, ctx)
    _ask(s, ctx, times=3)
    resp, rid = _ask(s, ctx)
    assert _aliases(calls) == ["mid", "mid", "mid", "fast", "mid"]
    assert resp == strong_prose and rid == "r5"          # проза сильной остаётся в силе
    (ev,) = _reroutes(events)
    assert ev["triggers"] == ["no_tool_call"] and ev["tools"] == []
    discarded = {k: ev[k] for k in ("discarded_calls", "discarded_text") if k in ev}
    assert discarded == ({"discarded_text": "Готово: тест исправлен."} if answer else {})
    assert s.last_routing_reason == "no_tool_call"


def test_the_answer_of_the_strong_model_is_never_rechecked(monkeypatch):
    """Переигранный ответ исполняется как есть, даже битый: второго переигрыша
    нет. Битый вызов сильной на её собственном шаге тоже идёт обычным путём."""
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    broken = [_fc("fs_delete_everything", "c0", json.dumps({"path": "src"}))]
    responses = [(broken, "r1", {})] + _reads(2, 3) + [([_write("c4")], "r4", {}), (broken, "r5", {})]
    calls = _stub_transport(monkeypatch, responses)
    events = _capture_events(monkeypatch, ctx)

    resp, rid = _ask(s, ctx)
    assert resp == broken and _aliases(calls) == ["mid"] and _reroutes(events) == []

    _ask(s, ctx, times=2)
    resp, rid = _ask(s, ctx)
    assert _aliases(calls) == ["mid", "mid", "mid", "fast", "mid"]
    assert resp == broken and rid == "r5" and len(_reroutes(events)) == 1


def test_command_runs_from_the_weak_model_are_not_replayed_and_reach_the_tools(monkeypatch):
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    run_tests = [_fc("exec_command", "c4", json.dumps({"command": "pytest -q"}))]
    calls = _stub_transport(monkeypatch, _reads(1, 3) + [(run_tests, "r4", {})])
    events = _capture_events(monkeypatch, ctx)
    executed = []
    monkeypatch.setattr(pr, "aapply_tool", as_coro(lambda **kw: executed.append(kw["call_id"]) or '{"ok": true}'))
    monkeypatch.setattr(pr, "amaybe_update_intent_scope_from_tool", as_coro(lambda **_kw: None))
    _ask(s, ctx, times=3)

    resp, rid = _ask(s, ctx)
    run_coro(pr.aprocess_tool_calls(resp, s, ctx, run_agent_func=None))      # следующий шаг run_agent

    assert _aliases(calls) == ["mid", "mid", "mid", "fast"] and rid == "r4"
    assert _reroutes(events) == [] and s.current_level == "light"
    assert executed == ["c4"]


def test_the_same_call_kit_is_not_a_replay_target(monkeypatch):
    """Сильная умерла посреди хода, а ближайший живой уровень выше слабой — тот же
    комплект вызова (слоты `light` и `max` указывают на один алиас): переигрыш
    был бы тем же запросом той же модели. Ответ слабой остаётся в силе, продления
    и события нет."""
    ctx = _ctx({"value": "auto"}, registry={
        "models": {"fast": {}, "mid": {}},
        "slots": {"light": "fast", "default": "mid", "max": "fast"},
    })
    s = _state(ctx)
    calls = _stub_transport(monkeypatch, _reads(1, 3) + [([_write("c4")], "r4", {})])
    events = _capture_events(monkeypatch, ctx)
    _ask(s, ctx, times=3)
    ctx.model_router.breaker.trip("mid", "5xx после ретраев")

    resp, rid = _ask(s, ctx)

    assert _aliases(calls) == ["mid", "mid", "mid", "fast"] and rid == "r4"
    assert s.current_level == "light" and s.last_routing_reason == "descend"
    assert _reroutes(events) == []


def test_without_a_live_target_the_weak_answer_stays_and_nothing_is_extended(monkeypatch):
    """Сильная и всё, что выше, в предохранителе: цели нет. Важный вызов слабой
    исполняется как есть, и серия слабой идёт своим счётом — продление
    относится только к вызовам не ниже сильной."""
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    calls = _stub_transport(monkeypatch, _reads(1, 3) + [([_write("c4")], "r4", {}), ([_write("c5")], "r5", {})])
    events = _capture_events(monkeypatch, ctx)
    _ask(s, ctx, times=3)
    for alias in ("mid", "big"):
        ctx.model_router.breaker.trip(alias, "5xx после ретраев")

    _ask(s, ctx, times=2)

    assert _aliases(calls) == ["mid", "mid", "mid", "fast", "fast"]
    assert _reroutes(events) == [] and s.steps_on_current_level == 2


# --- продление --------------------------------------------------------------------

def test_an_important_call_of_the_strong_model_extends_its_series(monkeypatch):
    """Важный вызов сильной на третьем шаге удержания: ещё `strong_steps − 1`
    шагов сильной, переигрыша нет."""
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    responses = _reads(1, 2) + [([_write("c3")], "r3", {})] + _reads(4, 6)
    calls = _stub_transport(monkeypatch, responses)
    events = _capture_events(monkeypatch, ctx)
    _ask(s, ctx, times=3)
    assert s.steps_on_current_level == 1                 # серия начата заново важным вызовом
    _ask(s, ctx, times=3)
    assert _aliases(calls) == ["mid", "mid", "mid", "mid", "mid", "fast"]
    assert _reroutes(events) == []




def test_a_broken_call_of_the_strong_model_neither_replays_nor_extends(monkeypatch):
    """Битый вызов сильной на третьем шаге удержания не обнуляет и не продлевает
    серию — продление держится только на важном туле; четвёртый вызов уходит на
    слабую по обычному расписанию. Проза сильной на первом шаге нового хода —
    тоже без переигрыша."""
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    bad = _fc("fs_read_file", "bad", json.dumps({"offset": 3}))
    calls = _stub_transport(monkeypatch, _reads(1, 2) + [([bad], "r3", {})] + _reads(4, 4))
    events = _capture_events(monkeypatch, ctx)
    _ask(s, ctx, times=3)
    assert s.steps_on_current_level == 3                 # серия НЕ обнулилась битым вызовом сильной
    _ask(s, ctx)
    assert _aliases(calls) == ["mid", "mid", "mid", "fast"]   # четвёртый — спуск по расписанию
    assert _reroutes(events) == []

    ctx2 = _ctx({"value": "auto"})
    s2 = _state(ctx2)
    strong_prose = [_prose("x")]
    calls2 = _stub_transport(monkeypatch, [(strong_prose, "p1", {})])
    resp, rid = _ask(s2, ctx2)
    assert _aliases(calls2) == ["mid"] and resp == strong_prose and s2.steps_on_current_level == 1


def test_an_important_call_next_to_a_broken_one_from_the_strong_model_extends(monkeypatch):
    """Важный вызов сильной в одном батче с битым вызовом всё равно продлевает
    серию — решает важность, соседняя ошибка схемы ей не помеха."""
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    bad = _fc("fs_read_file", "bad", json.dumps({"offset": 3}))
    mixed = [_write("w"), bad]
    calls = _stub_transport(monkeypatch, _reads(1, 2) + [(mixed, "r3", {})] + _reads(4, 4))
    _ask(s, ctx, times=3)
    assert s.steps_on_current_level == 1                  # серия начата заново важным вызовом
    _ask(s, ctx)
    assert _aliases(calls) == ["mid", "mid", "mid", "mid"]




# --- поток: канал `answer` у заведомо переигрываемого вызова --------------------------

def _muted(calls):
    return [call["muted"] for call in calls]


def test_only_the_weak_call_with_a_replay_target_is_muted(monkeypatch):
    """Вызовы сильной и сам переигранный вызов идут с полным потоком."""
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    calls = _stub_transport(monkeypatch, _reads(1, 3) + [([_prose("Готово.")], "r4", {}),
                                                         ([_prose("Готово, проверено.")], "r5", {})])
    _ask(s, ctx, times=4)
    assert _aliases(calls) == ["mid", "mid", "mid", "fast", "mid"]
    assert _muted(calls) == [False, False, False, True, False]


def test_a_weak_call_without_a_replay_target_is_not_muted(monkeypatch):
    """Цели нет — прозу слабой не переиграют, и пользователь обязан её видеть."""
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    calls = _stub_transport(monkeypatch, _reads(1, 4))
    _ask(s, ctx, times=3)
    for alias in ("mid", "big"):
        ctx.model_router.breaker.trip(alias, "5xx после ретраев")
    _ask(s, ctx)
    assert _aliases(calls) == ["mid", "mid", "mid", "fast"]
    assert _muted(calls) == [False, False, False, False]




def test_a_manual_level_is_never_muted(monkeypatch):
    ctx = _ctx({"value": "light"})
    s = _state(ctx)
    calls = _stub_transport(monkeypatch, _reads(1, 1))
    _ask(s, ctx)
    assert _aliases(calls) == ["fast"] and _muted(calls) == [False]


def _completion(call_id):
    """Ответ chat/completions с одним неважным вызовом — реальная форма бэкенда."""
    return {"id": f"chatcmpl-{call_id}", "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "", "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": "fs_read_file", "arguments": json.dumps({"path": f"src/{call_id}.py"})}}]}}]}


def test_the_muted_call_streams_thinking_and_tool_deltas_but_no_answer_text(monkeypatch):
    """Сквозной путь до приёмника потока: настоящая воронка запроса, замещён только
    транспорт. Текст ответа слабой модели в ленту не уходит, рассуждение и
    аргументы тулов — уходят; у сильной поток полный."""
    ctx = _ctx({"value": "auto"})
    s = _state(ctx)
    seen = []
    ctx.on_stream_delta = lambda kind, text: seen.append((kind, text))
    sent = []

    async def fake_policy(*, payload_factory, initial_max_tokens, on_delta=None, **_kw):
        model = payload_factory(initial_max_tokens)["model"]
        sent.append(model)
        for kind, text in (("reset", ""), ("think", f"{model}:думаю"),
                           ("answer", f"{model}:текст"), ("tool", f"{model}:аргументы")):
            on_delta(kind, text)
        return _completion(f"c{len(sent)}"), {"max_tokens": initial_max_tokens}

    monkeypatch.setattr(pr, "arequest_chat_completions_with_policy", fake_policy)

    _ask(s, ctx, times=4)

    assert sent == ["mid", "mid", "mid", "fast"]
    assert [d for d in seen if d[1].startswith("fast")] == [
        ("think", "fast:думаю"), ("tool", "fast:аргументы")]
    assert [d for d in seen if d[1].startswith("mid")] == [
        ("think", "mid:думаю"), ("answer", "mid:текст"), ("tool", "mid:аргументы")] * 3
    assert [d for d in seen if d[0] == "reset"] == [("reset", "")] * 4


def test_model_port_sends_the_models_selected_by_the_same_routing_state(monkeypatch):
    from gc_core.execution.local import LocalOperations

    ctx = _ctx({"value": "auto"}, auto_routing_two_level={"weak_steps": 2, "strong_steps": 2})
    s = _state(ctx)
    # Подготовленный рантайм использует общую границу хода, без предварительной
    # привязки состояния локальным executor.
    ctx.active_state = None
    pr.begin_turn_main_trace(s, ctx)
    ctx.operations = LocalOperations(ctx)
    actual = []

    async def transport(messages, state, call_ctx, *, resolved, **kwargs):
        actual.append(resolved.alias)
        return [_read("read-"+str(len(actual)))], "response-"+str(len(actual)), {}

    monkeypatch.setattr(pr, "_arequest_backend_canonical_once", transport)
    _ask(s, ctx, times=6)
    assert actual == ["mid", "mid", "fast", "fast", "mid", "mid"]
    assert ctx.active_state is s


def test_model_port_keeps_the_call_level_when_live_mode_changes_during_pair_judge(monkeypatch):
    import asyncio
    from gc_core.auto_routing.pair_judge import PairChoice, SOURCE_JUDGE
    from gc_core.execution.local import LocalOperations

    box = {"value": "auto"}
    ctx = _ctx(box, auto_routing_safe_level="auto")
    s = _state(ctx)
    ctx.operations = LocalOperations(ctx)
    actual = []
    judge_calls = []
    events = _capture_events(monkeypatch, ctx)

    async def pair(*args, **kwargs):
        judge_calls.append(True)
        await asyncio.sleep(0)
        box["value"] = "light"
        return PairChoice("max", 5, "hard", SOURCE_JUDGE, None)

    async def transport(messages, state, call_ctx, *, resolved, **kwargs):
        actual.append(resolved.alias)
        response = _write("first") if len(actual) == 1 else _read("next")
        return [response], "response-" + str(len(actual)), {}

    monkeypatch.setattr(pr, "arun_pair_judge", pair)
    monkeypatch.setattr(pr, "_arequest_backend_canonical_once", transport)

    response, _ = _ask(s, ctx)
    assert s._call_level == "max"
    assert actual == ["big"]
    assert response == [_write("first")]
    assert _reroutes(events) == []

    _ask(s, ctx)
    assert actual == ["big", "fast"]
    assert s._call_level == "light"
    assert judge_calls == [True]


@pytest.mark.parametrize("batch_kind", ["single", "invalid_first", "invalid_last"])
def test_malformed_json_from_strong_restarts_its_series(monkeypatch, batch_kind):
    """Отвергнутый до исполнения JSON удерживает сильную, включая смешанный батч."""
    ctx = _ctx({"value": "auto"}, auto_routing_two_level={"weak_steps": 10, "strong_steps": 2})
    state = _state(ctx)
    bad = _fc("fs_read_file", "bad", '{"path": ')
    good = _read("good")
    batch = {"single": [bad], "invalid_first": [bad, good], "invalid_last": [good, bad]}[batch_kind]
    finish = [_fc("turn_finish", "fin", FINISH_ARGUMENTS)]
    calls = _stub_transport(monkeypatch, [
        ([_read("c1")], "r1", {}), (batch, "r2", {}),
        ([_read("c3")], "r3", {}), (finish, "r4", {}), (finish, "r5", {}),
    ])
    executed = []
    async def apply(**kw):
        executed.append(kw["call_id"])
        return json.dumps({"ok": True})
    monkeypatch.setattr(pr, "aapply_tool", apply)
    monkeypatch.setattr(pr, "FinishGateGuard", _AcceptingGate)
    monkeypatch.setattr(pr, "amaybe_update_intent_scope_from_tool", as_coro(lambda **kw: None))

    run_coro(pr.arun_agent([{"role": "user", "content": "почини тест", "id": "u1"}], state, ctx))

    assert _aliases(calls) == ["mid", "mid", "mid", "mid"]
    assert "bad" not in executed
    if batch_kind != "single":
        assert executed.count("good") == 1
        from gc_core.utils.connect_backend import create_call_data_completions
        payload = create_call_data_completions([], state=state)
        good_index = next(i for i, m in enumerate(payload["messages"])
                          if any(c["id"] == "good" for c in m.get("tool_calls", [])))
        assert payload["messages"][good_index + 1]["role"] == "tool"
        assert payload["messages"][good_index + 1]["tool_call_id"] == "good"
    assert any("error_message" in str(m.get("content", "")) for m in state.messages)


def test_malformed_json_after_weak_replay_keeps_recovery_on_strong(monkeypatch):
    """Единственный floor replay не скрывает ошибку JSON уже от сильной модели."""
    ctx = _ctx({"value": "auto"}, auto_routing_two_level={"weak_steps": 10, "strong_steps": 2})
    state = _state(ctx)
    finish = [_fc("turn_finish", "fin", FINISH_ARGUMENTS)]
    calls = _stub_transport(monkeypatch, [
        ([_read("c1")], "r1", {}), ([_read("c2")], "r2", {}),
        ([_fc("fs_read_file", "weak_bad", '{"path": ')], "r3", {}),
        ([_fc("fs_read_file", "strong_bad", '{"path": ')], "r4", {}),
        ([_read("c5")], "r5", {}), ([_read("c6")], "r6", {}),
        (finish, "r7", {}), (finish, "r8", {}),
    ])
    executed = []
    async def apply(**kw):
        executed.append(kw["call_id"])
        return json.dumps({"ok": True})
    monkeypatch.setattr(pr, "aapply_tool", apply)
    monkeypatch.setattr(pr, "FinishGateGuard", _AcceptingGate)
    monkeypatch.setattr(pr, "amaybe_update_intent_scope_from_tool", as_coro(lambda **kw: None))

    run_coro(pr.arun_agent([{"role": "user", "content": "почини тест", "id": "u1"}], state, ctx))

    assert _aliases(calls) == ["mid", "mid", "fast", "mid", "mid", "mid", "fast", "mid"]
    assert "weak_bad" not in executed and "strong_bad" not in executed


def test_old_rejection_recovery_patch_keeps_previous_request_sequence(monkeypatch):
    """Durable runtime может сохранить прежнее поведение записанного такта."""
    ctx = _ctx({"value": "auto"}, auto_routing_two_level={"weak_steps": 10, "strong_steps": 2})
    ctx.execution_patch = lambda _patch: False
    state = _state(ctx)
    finish = [_fc("turn_finish", "fin", FINISH_ARGUMENTS)]
    calls = _stub_transport(monkeypatch, [
        ([_read("c1")], "r1", {}),
        ([_fc("fs_read_file", "bad", '{"path": ')], "r2", {}),
        (finish, "r3", {}), (finish, "r4", {}),
    ])
    monkeypatch.setattr(pr, "aapply_tool", as_coro(lambda **kw: json.dumps({"ok": True})))
    monkeypatch.setattr(pr, "FinishGateGuard", _AcceptingGate)
    monkeypatch.setattr(pr, "amaybe_update_intent_scope_from_tool", as_coro(lambda **kw: None))

    run_coro(pr.arun_agent([{"role": "user", "content": "почини тест", "id": "u1"}], state, ctx))

    assert _aliases(calls) == ["mid", "mid", "fast", "mid"]
