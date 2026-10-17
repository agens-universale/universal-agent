"""turn_propose_plan: сам тул и завершение хода по предложенному плану."""

from __future__ import annotations

import json

from gc_core import processing_responses as pr
from gc_core.auto_routing.pair_judge import build_pair_card
from gc_core.execution.state import SharedStateData
from gc_core.intent_log import append_user_ask
from gc_core.plan_store import PLAN_MAX_CHARS
from gc_core.processing_responses import arun_agent
from gc_core.security.permission_store import PermissionStore
from gc_core.states import State
from gc_core.tools.registry import get_builtin_tools_data
from gc_core.tools.turn.propose_plan import ProposePlan
from gc_testkit import run_coro
from gc_testkit.settings import fake_ctx, make_settings


def _state(mode="plan", **kw):
    state = State(
        verbose=False,
        model="CodeChat",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=5,
        system_prompt_id="v1",
        use_server_subagents=False,
        **kw,
    )
    state.shared = SharedStateData(permission_mode=mode)
    return state


def _ctx(state):
    return fake_ctx(
        {
            "backend.api_mode": "chat_completions",
            "agent.strict_turn_finish_validation": False,
            "agent.no_tool_call_max_count": 5,
        },
        logger=None,
        auth_connector=None,
        permission_store=PermissionStore(
            state.shared.confirm_rules, shared=state.shared
        ),
    )


def _call(state, plan):
    tool = ProposePlan(state=state, ctx=_ctx(state))
    return json.loads(run_coro(tool.acall(plan=plan)))


# --- тул ----------------------------------------------------------------------


def test_plan_is_stored_and_the_turn_is_flagged_for_closing():
    state = _state()
    out = _call(state, "  1. сделать\n2. проверить ")
    assert out["ok"] is True and out["status"] == "proposed"
    assert state.shared.plan["text"] == "1. сделать\n2. проверить"
    assert state.shared.plan["status"] == "proposed"
    assert state.plan_proposed is True


def test_outside_plan_mode_the_tool_refuses():
    for mode in ("edit", "manual"):
        state = _state(mode)
        out = _call(state, "план")
        assert out["ok"] is False
        assert out["error"]["type"] == "not_in_plan_mode"
        assert state.shared.plan is None and state.plan_proposed is False


def test_subagent_cannot_propose_a_plan():
    state = _state(parent_state_id="parent")
    out = _call(state, "план")
    assert out["error"]["type"] == "not_available_to_subagent"
    assert state.shared.plan is None


def test_empty_and_oversized_plans_are_rejected():
    state = _state()
    assert _call(state, "   ")["error"]["type"] == "invalid_argument"
    out = _call(state, "x" * (PLAN_MAX_CHARS + 1))
    assert out["error"]["type"] == "limit_exceeded"
    assert state.shared.plan is None and state.plan_proposed is False


def test_tool_is_registered_as_a_turn_tool():
    entry = get_builtin_tools_data(make_settings())["turn_propose_plan"]
    assert entry["category"] == "turn"


# --- цикл агента ----------------------------------------------------------------


def _fc(name, call_id, arguments="{}"):
    return {
        "id": f"id_{call_id}",
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }


def _patch_apply(monkeypatch, state, applied):
    async def fake_apply(**kw):
        name = kw["tool_name"]
        applied.append(name)
        if name == "turn_propose_plan":
            args = json.loads(kw["arguments"])
            return await ProposePlan(state=state, ctx=kw["ctx"]).acall(**args)
        return json.dumps({"tool": name, "ok": True}, ensure_ascii=False)

    async def _noop(**_kw):
        return None

    monkeypatch.setattr(pr, "aapply_tool", fake_apply)
    monkeypatch.setattr(
        pr, "maybe_store_and_wrap_tool_output", lambda res, **kw: res
    )
    monkeypatch.setattr(pr, "amaybe_update_intent_scope_from_tool", _noop)


def test_proposed_plan_closes_the_run_and_skips_the_rest_of_the_batch(
    monkeypatch,
):
    state = _state()
    applied: list[str] = []
    _patch_apply(monkeypatch, state, applied)
    persisted: list[dict] = []
    batch = [
        _fc("fs_read_file", "c1", '{"path":"a.py"}'),
        _fc("turn_propose_plan", "c2", json.dumps({"plan": "1. шаг"})),
        _fc("fs_write_file", "c3", '{"path":"a.py","content":"x"}'),
        _fc("turn_finish", "c4", "{}"),
    ]
    result = run_coro(
        arun_agent(
            batch,
            state,
            _ctx(state),
            persist_messages_callback=lambda msgs: persisted.extend(msgs),
        )
    )
    assert json.loads(result) == {"plan": "1. шаг", "status": "proposed"}
    assert applied == ["fs_read_file", "turn_propose_plan"]
    assert state.terminal_outcome == "plan_proposed"
    assert state.plan_proposed is False
    outputs = {
        m["call_id"]: m["output"]
        for m in state.messages
        if m.get("type") == "function_call_output"
    }
    assert set(outputs) == {"c1", "c2", "c3", "c4"}  # пары вызов-ответ целы
    for skipped in ("c3", "c4"):
        assert json.loads(outputs[skipped])["skipped"] is True
    assert {m.get("call_id") for m in persisted} >= {"c1", "c2", "c3", "c4"}


def test_plan_is_the_outcome_of_its_turn_for_the_next_pair_judge(
    monkeypatch,
):
    """Ход, закрытый планом, оставляет итог в журнале намерений: судья пары
    на ходе исполнения видит, какой план пойдёт в работу."""
    state = _state()
    state.current_user_intent = append_user_ask(
        [], "добавь экспорт отчёта", user_turn_seq=0
    )
    _patch_apply(monkeypatch, state, [])
    batch = [_fc("turn_propose_plan", "c1", json.dumps({"plan": "1. шаг"}))]
    run_coro(arun_agent(batch, state, _ctx(state)))

    previous = json.loads(build_pair_card(state))["previous_turn"]
    assert previous == {
        "request": "добавь экспорт отчёта",
        "outcome": "plan: 1. шаг",
    }


def test_refused_proposal_does_not_close_the_run(monkeypatch):
    """Вне plan тул отвечает отказом — ход не закрывается, остаток батча идёт."""
    state = _state("edit")
    applied: list[str] = []
    _patch_apply(monkeypatch, state, applied)
    outs = run_coro(
        pr.aprocess_tool_calls(
            [
                _fc("turn_propose_plan", "c1", json.dumps({"plan": "п"})),
                _fc("fs_read_file", "c2", '{"path":"a.py"}'),
            ],
            state,
            _ctx(state),
            run_agent_func=None,
        )
    )
    assert applied == ["turn_propose_plan", "fs_read_file"]
    assert state.plan_proposed is False
    assert all("skipped" not in m.get("output", "") for m in outs)


def test_model_sees_the_tool_only_in_plan_mode_and_only_as_the_root_agent():
    """В режиме по умолчанию запросы к модели остаются прежними."""
    from gc_core.security.permission_mode import tools_for_mode

    data = get_builtin_tools_data(make_settings())
    base = [{"name": "fs_read_file"}]

    def names(state):
        ctx = _ctx(state)
        ctx.tools_data = data
        return [t["name"] for t in tools_for_mode(base, ctx, state)]

    assert names(_state("plan")) == ["fs_read_file", "turn_propose_plan"]
    for mode in ("edit", "manual"):
        assert names(_state(mode)) == ["fs_read_file"]
    assert names(_state("plan", parent_state_id="p")) == ["fs_read_file"]


def test_plan_tool_is_offered_by_the_tool_set_of_the_agent():
    """Есть ли у агента тул плана в plan — тем же правилом, что набор
    тулов запроса, в любом текущем режиме."""
    from gc_core.security.permission_mode import offers_plan_tool

    data = get_builtin_tools_data(make_settings())
    for state in (_state("edit"), _state("plan")):
        ctx = _ctx(state)
        ctx.tools_data = data
        assert offers_plan_tool(ctx, state) is True
    child = _state("plan", parent_state_id="p")
    ctx = _ctx(child)
    ctx.tools_data = data
    assert offers_plan_tool(ctx, child) is False
    ctx.tools_data = {}
    assert offers_plan_tool(ctx, _state("plan")) is False


def test_tool_is_absent_from_the_common_tool_list():
    """Общий список схем уходит в системный промпт сессии — тул режима туда
    не попадает, иначе запросы в edit отличались бы от прежних."""
    from gc_core.tools import abuild_tools_data

    tools_data, tools_spec, _descr, _mcp = run_coro(
        abuild_tools_data(make_settings())
    )
    assert "turn_propose_plan" in tools_data
    assert "turn_propose_plan" not in [spec["name"] for spec in tools_spec]


# --- показать сохранённый план снова --------------------------------------------


def _show(state):
    tool = ProposePlan(state=state, ctx=_ctx(state))
    return json.loads(run_coro(tool.acall(plan="", show_current=True)))


def test_show_current_presents_the_stored_plan_unchanged():
    """«Покажи план снова»: текст, статус и дата целы — утверждённый план не
    должен сбрасываться в «предложен» из-за просьбы на него посмотреть."""
    state = _state()
    _call(state, "1. шаг")
    state.shared.plan.update(status="approved", approved_mode="edit")
    before = dict(state.shared.plan)
    state.plan_proposed = False

    out = _show(state)
    assert out["ok"] is True and out["status"] == "approved"
    assert state.shared.plan == before
    assert (
        state.plan_proposed is True
    )  # ход закрывается, человек получает выбор


def test_show_current_is_refused_without_a_plan_and_under_rework():
    state = _state()
    assert _show(state)["error"]["type"] == "no_plan"
    _call(state, "1. шаг")
    state.shared.plan["status"] = "reworking"
    state.plan_proposed = False
    assert _show(state)["error"]["type"] == "plan_under_rework"
    assert state.plan_proposed is False


def test_empty_plan_without_the_flag_is_still_rejected():
    """Пустая строка — не сигнал «покажи прежний»: иначе модель, написавшая
    новый план прозой, подняла бы выбор над старым текстом."""
    state = _state()
    _call(state, "1. шаг")
    state.plan_proposed = False
    assert _call(state, "")["error"]["type"] == "invalid_argument"
    assert state.plan_proposed is False


def test_the_rule_about_showing_the_plan_again_has_three_carriers():
    from gc_core.message_policy.plan_messages import PLAN_ENTER_MESSAGE

    spec = ProposePlan.tool_description()
    assert "show_current" in spec["parameters"]["properties"]
    assert spec["parameters"]["required"] == ["plan"]
    for text in (
        spec["description"],
        ProposePlan.prompt_description(),
        PLAN_ENTER_MESSAGE,
    ):
        assert "show_current" in text
        assert "Call once" not in text


def test_run_result_carries_the_stored_status(monkeypatch):
    state = _state()
    _call(state, "1. шаг")
    state.shared.plan.update(status="approved", approved_mode="edit")
    state.plan_proposed = False
    applied: list[str] = []

    async def fake_apply(**kw):
        applied.append(kw["tool_name"])
        return await ProposePlan(state=state, ctx=kw["ctx"]).acall(
            **json.loads(kw["arguments"])
        )

    async def _noop(**_kw):
        return None

    monkeypatch.setattr(pr, "aapply_tool", fake_apply)
    monkeypatch.setattr(
        pr, "maybe_store_and_wrap_tool_output", lambda res, **kw: res
    )
    monkeypatch.setattr(pr, "amaybe_update_intent_scope_from_tool", _noop)
    result = run_coro(
        arun_agent(
            [
                _fc(
                    "turn_propose_plan",
                    "c1",
                    json.dumps({"plan": "", "show_current": True}),
                )
            ],
            state,
            _ctx(state),
            persist_messages_callback=lambda msgs: None,
        )
    )
    assert json.loads(result) == {"plan": "1. шаг", "status": "approved"}
    assert state.terminal_outcome == "plan_proposed"


# --- показ плана фронтендам -------------------------------------------------


def test_present_result_is_the_plan_text_of_a_turn_closed_by_a_plan(
    monkeypatch,
):
    """Строка итога — от настоящего цикла, а не её копия в тесте: писатель
    и читатель формата не разойдутся молча."""
    state = _state()
    _patch_apply(monkeypatch, state, [])
    result = run_coro(
        arun_agent(
            [_fc("turn_propose_plan", "c1", json.dumps({"plan": "1. шаг"}))],
            state,
            _ctx(state),
        )
    )
    assert ProposePlan.present_result(result) == "1. шаг"


def test_present_result_is_none_for_anything_but_a_plan_turn():
    finish_args = json.dumps(
        {"solution_type": "full_solution", "task_report": "готово"}
    )
    for raw in (
        None,
        42,
        "",
        "готово",
        "{не json",
        json.dumps(["plan"]),
        finish_args,
        json.dumps({"plan": "1. шаг"}),
        json.dumps({"plan": "  ", "status": "proposed"}),
        json.dumps({"plan": "1. шаг", "status": "что-то"}),
        json.dumps({"plan": "1. шаг", "status": "proposed", "ok": True}),
    ):
        assert ProposePlan.present_result(raw) is None


def test_plan_from_arguments_reads_the_tool_call():
    """Повтор истории (ACP) видит в треде только вызов тула: итог прогона в
    тред не пишется. Аргументы — строка JSON или dict."""
    assert (
        ProposePlan.plan_from_arguments(json.dumps({"plan": "1. шаг"}))
        == "1. шаг"
    )
    assert ProposePlan.plan_from_arguments({"plan": "1. шаг"}) == "1. шаг"
    for raw in (
        None,
        "",
        "{не json",
        json.dumps({"plan": "  ", "show_current": True}),
        {"plan": "", "show_current": True},
        {"plan": 7},
        json.dumps(["plan"]),
    ):
        assert ProposePlan.plan_from_arguments(raw) is None
