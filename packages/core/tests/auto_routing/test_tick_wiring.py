"""Проводка такта (спека §4): оценки читаются одной точкой, сигналы пишутся всегда,
такт закрывается успешным ответом."""

import json
from types import SimpleNamespace

from gc_core import processing_responses as pr
from gc_core.auto_routing.policy import (
    LOOP,
    NO_TOOL_BOUNCE,
    TOOL_CALL_ERROR,
    FINISH_REJECT,
)
from gc_core.states import State
from gc_core.turn_boundary import begin_turn
from gc_testkit import as_coro, run_coro
from gc_testkit.settings import fake_ctx, make_settings


def _state():
    return State(
        verbose=False,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=5,
        system_prompt_id="v1",
        use_server_subagents=False,
    )


def _ctx_with_tool(injected=("service_marker",)):
    entry = {
        "spec": {
            "name": "x",
            "parameters": {
                "type": "object",
                "properties": {"p": {"type": "string"}},
                "required": ["p"],
            },
        },
        "description": "x",
        "tool_fabric": None,
        "injected_fields": list(injected),
    }
    return fake_ctx(tools_data={"x": entry}, logger=None)


def _assistant_with_calls(*arg_dicts):
    return [
        {
            "type": "function_call",
            "name": "x",
            "call_id": f"c{i}",
            "arguments": json.dumps(a),
        }
        for i, a in enumerate(arg_dicts)
    ]


def test_state_starts_with_routing_defaults_and_begin_turn_resets():
    from gc_core.turn_boundary import begin_turn

    s = _state()
    s.current_level, s.steps_in_turn, s.tick_signals = "max", 5, {LOOP}
    begin_turn(
        s, SimpleNamespace()
    )  # без Router — no-op по модели, но сброс роутинга
    assert (
        s.current_level is None
        and s.steps_in_turn == 0
        and s.tick_signals == set()
    )


def test_close_tick_called_after_successful_main_call(monkeypatch):
    s = _state()
    ctx = fake_ctx(logger=None)
    monkeypatch.setattr(
        pr, "aprepare_main_call", as_coro(lambda state, ctx: None)
    )
    monkeypatch.setattr(
        pr,
        "_arequest_backend_canonical",
        as_coro(
            lambda messages, state, ctx, *, resolved, set_previous_response_id=True, mute_answer=False: (
                [],
                "rid",
                {},
            )
        ),
    )
    s.tick_signals = {LOOP}
    run_coro(pr.aprocess_user_ask([], s, ctx))
    assert s.steps_in_turn == 1 and s.tick_signals == set()


def test_failed_main_call_does_not_close_tick(monkeypatch):
    s = _state()
    ctx = fake_ctx(logger=None)
    monkeypatch.setattr(
        pr, "aprepare_main_call", as_coro(lambda state, ctx: None)
    )

    async def boom(*a, **k):
        raise RuntimeError("backend down")

    monkeypatch.setattr(pr, "_arequest_backend_canonical", boom)
    s.tick_signals = {LOOP}
    try:
        run_coro(pr.aprocess_user_ask([], s, ctx))
    except RuntimeError:
        pass
    assert s.steps_in_turn == 0 and s.tick_signals == {LOOP}


def test_pending_mid_turn_pickup_does_not_reset_steps_in_turn(monkeypatch):
    """Pending, подхваченный в
    начале ЛЮБОЙ итерации `run_agent` (`get_pending_messages`, проверяется на
    каждом обороте цикла) — не новый ход и не граница auto: счётчик такта
    (`steps_in_turn`) продолжает расти через тики, а не обнуляется. Отличие от
    `_pickup_pending_as_new_turn` (см. `test_pending_after_compaction.py`) —
    тот срабатывает только на finish/text-accept, этот путь — вообще без
    завершения хода."""
    from gc_core.auto_routing import state_ops
    from gc_core.constants.tool_names import FINISH_TOOL_NAME

    s = _state()
    ctx = fake_ctx(logger=None)

    calls = {"n": 0}

    async def fake_process_user_ask(messages, state, ctx):
        calls["n"] += 1
        state_ops.close_tick(
            state, "default"
        )  # реальный тик такта на каждый half-turn
        if calls["n"] == 1:
            return [
                {"role": "assistant", "content": "промежуточный ответ"}
            ], "rid1"
        finish = [
            {
                "type": "function_call",
                "name": FINISH_TOOL_NAME,
                "call_id": "cf",
                "arguments": json.dumps({}),
            }
        ]
        return finish, "rid2"

    monkeypatch.setattr(pr, "aprocess_user_ask", fake_process_user_ask)
    # Develop-батч реально исполняет turn_finish через aapply_tool; у ctx здесь
    # нет tools_data — стабим aapply_tool, как в test_batch_tools_before_turn_finish.py.
    monkeypatch.setattr(
        pr,
        "aapply_tool",
        as_coro(
            lambda **kw: json.dumps({"tool": kw["tool_name"], "ok": True})
        ),
    )

    pending_calls = {"n": 0}

    def _get_pending():
        pending_calls["n"] += 1
        if pending_calls["n"] == 2:  # top-of-loop проверка 2-й итерации
            return [
                {"role": "user", "content": "второе сообщение", "id": "m2"}
            ]
        return []

    result = run_coro(
        pr.arun_agent(
            [{"role": "user", "content": "первое сообщение"}],
            s,
            ctx,
            get_pending_messages=_get_pending,
        )
    )

    assert result == json.dumps({})
    assert calls["n"] == 2  # оба тика реально произошли
    assert s.steps_in_turn == 2  # НЕ обнулилось pending-подхватом


def _finish_ctx(injected):
    import logging
    from gc_core.constants.tool_names import FINISH_TOOL_NAME

    entry = {
        "spec": {
            "name": FINISH_TOOL_NAME,
            "parameters": {"type": "object", "properties": {}},
        },
        "description": "finish",
        "tool_fabric": None,
        "injected_fields": list(injected),
    }
    return fake_ctx(
        tools_data={FINISH_TOOL_NAME: entry},
        settings=make_settings(
            {"backend.api_mode": "chat_completions"}
        ).settings,
        logger=logging.getLogger("test_tick_wiring"),
        auth_connector=None,
    )


def _run_finish_through_the_gate(monkeypatch, ctx, arguments):
    """`run_agent` на пути chat_completions с одним `turn_finish`: судья finish
    gate подменён захватом, intent-scope — тоже. Возвращает (результат хода,
    аргументы у судьи, аргументы у intent-scope)."""
    from gc_core.constants.tool_names import FINISH_TOOL_NAME
    from gc_core.intent_guard.guard import FinishGateDecision

    judged, scoped = [], []

    class _Guard:
        enabled = False

        def __init__(self, **_kwargs):
            pass

        async def aevaluate_turn_finish(self, *, finish_handoff_arguments):
            judged.append(finish_handoff_arguments)
            return FinishGateDecision(outcome="accept")

    monkeypatch.setattr(pr, "FinishGateGuard", _Guard)
    monkeypatch.setattr(
        pr,
        "aapply_tool",
        as_coro(
            lambda **kw: json.dumps({"tool": kw["tool_name"], "ok": True})
        ),
    )
    monkeypatch.setattr(
        pr,
        "amaybe_update_intent_scope_from_tool",
        as_coro(lambda **kw: scoped.append(kw["tool_arguments"])),
    )
    s = _state()
    s.backend_api_mode = "chat_completions"
    finish = {
        "type": "function_call",
        "name": FINISH_TOOL_NAME,
        "call_id": "cf",
        "arguments": arguments,
    }
    result = run_coro(pr.arun_agent([finish], s, ctx))
    return result, judged, scoped


def test_finish_arguments_leave_the_turn_without_routing_fields(monkeypatch):
    """Возврат хода (печать gc-cli, `result` REST, ответ субагента родителю),
    судья finish gate и intent-scope получают аргументы `turn_finish` без
    служебных полей — одной точкой, помощником `prepare_tool_arguments`."""
    ctx = _finish_ctx(("service_marker",))
    raw = json.dumps({"service_marker": 9, "task_report": "done"})

    result, judged, scoped = _run_finish_through_the_gate(
        monkeypatch, ctx, raw
    )

    assert json.loads(result) == {"task_report": "done"}
    assert [json.loads(a) for a in judged] == [{"task_report": "done"}]
    assert [json.loads(a) for a in scoped] == [{"task_report": "done"}]


def test_finish_arguments_without_service_fields_stay_byte_for_byte(
    monkeypatch,
):
    """Инжектированных полей нет (ни роутинга, ни `inject_tool_fields`) — строка модели уходит как есть."""
    raw = '{"task_report":  "done"}'
    result, judged, _scoped = _run_finish_through_the_gate(
        monkeypatch, _finish_ctx(()), raw
    )
    assert result == raw and judged == [raw]


def test_the_turn_marker_is_set_when_the_first_messages_of_the_turn_reach_history(
    monkeypatch,
):
    """Граница хода снимает маркер, а ставится он там, где первые сообщения хода
    входят в историю: на старте рана историю треда дочитывают в `state.messages`
    уже ПОСЛЕ `begin_turn`, и пред-стартовая компакция её ещё и переписывает —
    длина на момент границы ходу ничего не говорит."""
    from gc_core.constants.tool_names import FINISH_TOOL_NAME

    s = _state()
    s.messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "прошлый ход"},
    ]
    begin_turn(s, SimpleNamespace())
    assert s.turn_messages_start is None

    finish = [
        {
            "type": "function_call",
            "name": FINISH_TOOL_NAME,
            "call_id": "cf",
            "arguments": json.dumps({}),
        }
    ]
    monkeypatch.setattr(
        pr,
        "aprocess_user_ask",
        as_coro(lambda messages, state, ctx: (finish, "rid")),
    )
    monkeypatch.setattr(
        pr,
        "aapply_tool",
        as_coro(
            lambda **kw: json.dumps({"tool": kw["tool_name"], "ok": True})
        ),
    )
    run_coro(
        pr.arun_agent(
            [{"role": "user", "content": "новый ход"}],
            s,
            fake_ctx(tools_data={}, logger=None),
        )
    )

    assert s.turn_messages_start == 2
    assert s.messages[2]["content"] == "новый ход"
