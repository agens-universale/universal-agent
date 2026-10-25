"""RunContext: ModelRouter + choice_provider + согласованный выбор модели вызова.

См. .superpowers/sdd/2026-07-27-profiles-wave-a-plan/task-5-brief.md.
"""

from types import SimpleNamespace

import pytest
from gc_core import run_context as rc
from gc_core.config_modules.root import AppConfig
from gc_core.model_router import ModelRouter
from gc_core.states import State
from gc_core.turn_boundary import begin_turn
from gc_testkit import as_coro, run_coro
from gc_testkit.settings import fake_ctx, make_settings


@pytest.fixture
def minimal_settings(monkeypatch):
    monkeypatch.setattr(
        rc,
        "abuild_tools_data",
        as_coro(lambda cfg, on_progress=None, **_kwargs: ({}, [], [], {})),
    )
    return make_settings(
        {"agent.model": "M", "level": "max", "logging.save_logs": False}
    )


def _snapshot(raw: dict) -> AppConfig:
    """Настоящие провалидированные настройки (``AppConfig``), не мок."""
    return AppConfig.model_validate(raw)


def _make_state(model: str = "unused-legacy-default") -> State:
    return State(
        verbose=False,
        model=model,
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=5,
        system_prompt_id="v1",
        use_server_subagents=False,
    )


# --- create_run_context: model_router + choice_provider ---------------------------


async def test_create_run_context_without_choice_provider_reads_config_level(
    minimal_settings,
):
    ctx = await rc.acreate_run_context(minimal_settings)
    assert isinstance(ctx.model_router, ModelRouter)
    assert ctx.model_router.active_target() == "max"


async def test_create_run_context_with_choice_provider_uses_callable(
    minimal_settings,
):
    level = {"value": "light"}
    ctx = await rc.acreate_run_context(
        minimal_settings, choice_provider=lambda: level["value"]
    )
    assert ctx.model_router.active_target() == "light"
    level["value"] = "default"
    assert (
        ctx.model_router.active_target() == "default"
    )  # провайдер живой, не закэширован


async def test_create_run_context_model_router_resolves_legacy_main(
    minimal_settings,
):
    ctx = await rc.acreate_run_context(minimal_settings)
    resolved = ctx.model_router.resolve("main")
    assert resolved.model == "M"


async def test_create_run_context_force_legacy_propagates_to_router(
    minimal_settings,
):
    """Task 10: `--model` при непустом реестре -> `force_legacy=True` доезжает от
    CLI до ModelRouter через create_run_context (спека §2)."""
    ctx = await rc.acreate_run_context(minimal_settings, force_legacy=True)
    resolved = ctx.model_router.resolve("main")
    assert resolved.model == "M"
    assert resolved.source == "легаси-форс --model"


async def test_create_run_context_default_force_legacy_is_false(
    minimal_settings,
):
    ctx = await rc.acreate_run_context(minimal_settings)
    resolved = ctx.model_router.resolve("main")
    assert resolved.source == "легаси"


# --- begin_turn: юнит + фиксация на ход -------------------------------------------


def test_begin_turn_sets_default_model_from_router_resolve_main(
    minimal_settings,
):
    router = ModelRouter(minimal_settings, choice_provider=lambda: "max")
    ctx = SimpleNamespace(model_router=router)
    state = _make_state()

    begin_turn(state, ctx)

    assert state.default_model == "M"


def test_begin_turn_is_noop_without_model_router():
    """Легаси-совместимость: ctx без model_router (тестовые двойники) не должен падать —
    default_model остаётся тем, что уже стоит на state."""
    ctx = fake_ctx({"agent.model": "X"})
    state = _make_state()

    begin_turn(state, ctx)

    assert state.default_model == "unused-legacy-default"


def test_begin_turn_refixes_model_between_turns_when_level_changes():
    """Спека §6.4: смена активного уровня меняет default_model следующего хода —
    только через явный begin_turn, не сама по себе внутри текущего хода."""
    snapshot = _snapshot(
        {
            "models": {
                "fast": {"url": "http://fast/v1", "token": ""},
                "smart": {"url": "http://smart/v1", "token": ""},
            },
            "slots": {"light": "fast", "max": "smart"},
        }
    )
    level = {"value": "light"}
    router = ModelRouter(snapshot, choice_provider=lambda: level["value"])
    ctx = SimpleNamespace(model_router=router)
    state = _make_state()

    begin_turn(state, ctx)
    assert state.default_model == "fast"

    # Лямбда меняется посреди хода — модель уже зафиксирована, не перечитывается.
    level["value"] = "max"
    assert state.default_model == "fast"

    # begin_turn следующего хода снимает актуальное значение.
    begin_turn(state, ctx)
    assert state.default_model == "smart"


# --- точка вызова: один резолв на вызов, живой уровень -------------------------


def _two_slot_router(
    level_box: dict, *, is_subagent_child=False
) -> ModelRouter:
    snapshot = _snapshot(
        {
            "models": {
                "fast": {"url": "http://fast/v1", "token": ""},
                "smart": {"url": "http://smart/v1", "token": ""},
            },
            "slots": {"light": "fast", "max": "smart"},
        }
    )
    return ModelRouter(
        snapshot,
        choice_provider=lambda: level_box["value"],
        is_subagent_child=is_subagent_child,
    )


def _main_ctx(router) -> SimpleNamespace:
    return SimpleNamespace(
        model_router=router,
        config_dict={},
        auth_connector=None,
        logger=None,
        is_tui=False,
    )


def test_resolve_main_for_call_follows_live_level_on_each_call():
    """До открытия ручного хода резолв читает живой уровень каждого вызова."""
    from gc_core import processing_responses as pr

    level = {"value": "light"}
    ctx = _main_ctx(_two_slot_router(level))
    state = _make_state()

    assert pr.resolve_main_for_call(state, ctx).alias == "fast"
    assert state.default_model == "fast"

    level["value"] = "max"  # /level посреди хода
    resolved = pr.resolve_main_for_call(state, ctx)
    assert resolved.alias == "smart"
    assert resolved.url == "http://smart/v1"
    assert state.default_model == "smart"


def test_resolve_main_for_call_is_none_without_router():
    from gc_core import processing_responses as pr

    state = _make_state()
    assert (
        pr.resolve_main_for_call(state, SimpleNamespace(config_dict={}))
        is None
    )
    assert state.default_model == "unused-legacy-default"


def test_resolve_main_for_call_open_alias_raises_honest_class2():
    """Открытый алиас активной модели отказывает до транспорта честной ошибкой
    (модель/адрес/подсказка), не сырым AliasCircuitOpenError."""
    from gc_core import processing_responses as pr
    from gc_core.model_router import AliasCircuitOpenError

    level = {"value": "light"}
    router = _two_slot_router(level)
    router.breaker.trip("fast", "5xx после ретраев")
    with pytest.raises(RuntimeError) as excinfo:
        pr.resolve_main_for_call(_make_state(), _main_ctx(router))
    assert not isinstance(excinfo.value, AliasCircuitOpenError)
    assert "/level" in str(excinfo.value)


def test_resolve_main_for_call_keeps_subagent_override():
    """Дочерний Router субагента с override резолвится в override, а не в слот
    живого уровня родителя."""
    from gc_core import processing_responses as pr

    level = {"value": "light"}
    parent = _two_slot_router(level)
    child = parent.with_override(parent.resolve_level("max"))
    resolved = pr.resolve_main_for_call(_make_state(), _main_ctx(child))
    assert resolved.alias == "smart"


def test_request_uses_resolved_passed_from_decision_point(monkeypatch):
    """Комплект вызова (url/токен/модель) — из ОДНОГО резолва точки решения:
    /level между решением и отправкой не уводит запрос на другой слот."""
    from gc_core import processing_responses as pr

    captured = {}

    async def fake_once(
        messages,
        state,
        ctx,
        *,
        resolved,
        set_previous_response_id=True,
        mute_answer=False,
    ):
        captured["resolved"] = resolved
        return [], "rid", {}

    monkeypatch.setattr(pr, "_arequest_backend_canonical_once", fake_once)
    level = {"value": "light"}
    ctx = _main_ctx(_two_slot_router(level))
    state = _make_state()
    resolved = pr.resolve_main_for_call(state, ctx)
    level["value"] = "max"  # /level после решения, до отправки
    run_coro(pr._arequest_backend_canonical([], state, ctx, resolved=resolved))
    assert captured["resolved"] is resolved
    assert captured["resolved"].alias == "fast"


def test_is_subagent_child_is_an_explicit_flag_not_derived_from_override(
    minimal_settings,
):
    """Спека auto-routing §5.1: признак «дочерний Router» задаётся явно — override у
    ребёнка станет необязательным (непривязанный субагент ведёт свой роутинг)."""
    parent = ModelRouter(minimal_settings, choice_provider=lambda: "max")
    assert parent.is_subagent_child is False

    child = ModelRouter(
        minimal_settings,
        choice_provider=lambda: "max",
        is_subagent_child=True,
    )
    assert child.is_subagent_child is True

    with_override = parent.with_override(parent.resolve("main"))
    assert (
        with_override.is_subagent_child is False
    )  # override сам по себе не делает ребёнком

    child_override = child.with_override(child.resolve("main"))
    assert child_override.is_subagent_child is True  # клон переносит флаг


# --- select_tools_for_call: страховка от пустой базы ---------------------------------


# --- prepare_main_call: резолв + выбор инструментов одним вызовом -------------------


def test_process_user_ask_calls_prepare_main_call(monkeypatch):
    from gc_core import processing_responses as pr

    calls = []
    monkeypatch.setattr(
        pr,
        "aprepare_main_call",
        as_coro(
            lambda state, ctx: (
                calls.append((state, ctx)) or "resolved-sentinel"
            )
        ),
    )

    async def fake_request(
        messages,
        state,
        ctx,
        *,
        resolved,
        set_previous_response_id=True,
        mute_answer=False,
    ):
        assert resolved == "resolved-sentinel"
        return [], "rid", {}

    monkeypatch.setattr(pr, "_arequest_backend_canonical", fake_request)
    state = _make_state()
    ctx = SimpleNamespace()

    run_coro(pr.aprocess_user_ask([], state, ctx))

    assert len(calls) == 1
    assert calls[0] == (state, ctx)


def test_process_tool_outputs_calls_prepare_main_call(monkeypatch):
    from gc_core import processing_responses as pr

    calls = []
    monkeypatch.setattr(
        pr,
        "aprepare_main_call",
        as_coro(
            lambda state, ctx: (
                calls.append((state, ctx)) or "resolved-sentinel"
            )
        ),
    )

    async def fake_request(
        messages,
        state,
        ctx,
        *,
        resolved,
        set_previous_response_id=True,
        mute_answer=False,
    ):
        assert resolved == "resolved-sentinel"
        return [], "rid", {}

    monkeypatch.setattr(pr, "_arequest_backend_canonical", fake_request)
    state = _make_state()
    ctx = SimpleNamespace()

    run_coro(pr.aprocess_tool_outputs([], state, ctx))

    assert len(calls) == 1
    assert calls[0] == (state, ctx)


def test_prepare_main_call_resolves_once_and_keeps_canonical_tools():
    from gc_core import processing_responses as pr

    level = {"value": "light"}
    ctx = _main_ctx(_two_slot_router(level))
    state = _make_state()
    canonical = [{"name": "fs_read_file"}]
    state.tools = canonical
    resolved = run_coro(pr.aprepare_main_call(state, ctx))
    assert resolved.alias == "fast"
    assert state.default_model == "fast"
    assert state.tools is canonical


def test_prepared_call_keeps_its_model_then_the_next_call_uses_live_selection():
    from gc_core import processing_responses as pr
    from gc_core.auto_routing import state_ops

    level = {"value": "light"}
    ctx = _main_ctx(_two_slot_router(level))
    state = _make_state()
    begin_turn(state, ctx)
    first = run_coro(pr.aprepare_main_call(state, ctx))
    assert first.alias == "fast"
    level["value"] = "max"
    assert run_coro(pr.aprepare_main_call(state, ctx)) == first
    assert first.url == "http://fast/v1"
    state_ops.close_tick(state, "light")
    assert run_coro(pr.aprepare_main_call(state, ctx)).alias == "smart"


def test_begin_turn_clears_a_legacy_manual_pin():
    state = _make_state()
    state.turn_level = "light"
    level = {"value": "max"}
    ctx = SimpleNamespace(model_router=_two_slot_router(level))
    begin_turn(state, ctx)
    assert state.turn_level is None
    assert state.default_model == "smart"


def test_prepared_main_resolve_still_checks_circuit_breaker():
    from gc_core import processing_responses as pr

    level = {"value": "light"}
    router = _two_slot_router(level)
    ctx = _main_ctx(router)
    state = _make_state()
    begin_turn(state, ctx)
    run_coro(pr.aprepare_main_call(state, ctx))
    router.breaker.trip("fast", "5xx после ретраев")
    with pytest.raises(RuntimeError, match="fast.*недоступна"):
        run_coro(pr.aprepare_main_call(state, ctx))


def test_main_call_keeps_subagent_override_when_parent_level_changes():
    from gc_core import processing_responses as pr
    from gc_core.auto_routing import state_ops

    level = {"value": "light"}
    parent = _two_slot_router(level)
    child = _two_slot_router(level, is_subagent_child=True).with_override(
        parent.resolve_level("max")
    )
    state = _make_state()
    ctx = _main_ctx(child)
    begin_turn(state, ctx)
    assert run_coro(pr.aprepare_main_call(state, ctx)).alias == "smart"
    level["value"] = "default"
    state_ops.close_tick(state, "light")
    assert run_coro(pr.aprepare_main_call(state, ctx)).alias == "smart"


def test_auto_turn_does_not_pin_the_manual_level(monkeypatch):
    from gc_core import processing_responses as pr

    level = {"value": "light"}
    router = _two_slot_router(level)
    monkeypatch.setattr(router, "live_choice", lambda: "auto")
    ctx = _main_ctx(router)
    ctx.auto_available = True
    state = _make_state()
    begin_turn(state, ctx)
    assert state.turn_level is None
    assert pr.resolve_main_for_call(state, ctx).alias == "fast"
    level["value"] = "max"
    assert pr.resolve_main_for_call(state, ctx).alias == "smart"


async def test_create_run_context_uses_the_virtual_pair_prompt_store(
    minimal_settings,
):
    from gc_testkit.memory_agent_store import MemoryAgentStore

    cfg = minimal_settings.with_overrides(
        {
            "models": {"fast": {}, "mid": {}, "big": {}},
            "slots": {"light": "fast", "default": "mid", "max": "big"},
            "backend.api_mode": "chat_completions",
            "level": "auto",
            "paths.chat_completions_routing_pair_judge_prompt_path": "/virtual/pair-prompt.md",
        }
    ).settings
    store = MemoryAgentStore(
        prompts={"routing_pair_judge": "Choose the initial pair."}
    )
    ctx = await rc.acreate_run_context(cfg, agent_store=store)
    assert ctx.agent_store is store
    assert ctx.auto_available is True
    assert ("prompt", "routing_pair_judge") not in store.reads


@pytest.mark.parametrize(
    "updates,force_legacy",
    [
        ({"agent.auto_routing": False}, False),
        ({"backend.api_mode": "responses"}, False),
        ({}, True),
    ],
)
async def test_create_run_context_skips_irrelevant_bad_pair_prompt(
    minimal_settings, updates, force_legacy
):
    cfg = minimal_settings.with_overrides(
        {
            "models": {"fast": {}, "mid": {}, "big": {}},
            "slots": {"light": "fast", "default": "mid", "max": "big"},
            "backend.api_mode": "chat_completions",
            "level": "auto",
            "paths.chat_completions_routing_pair_judge_prompt_path": "/virtual/no-prompt.md",
            **updates,
        }
    ).settings
    ctx = await rc.acreate_run_context(cfg, force_legacy=force_legacy)
    assert ctx.auto_available is False


@pytest.mark.parametrize(
    "source_level,selected_level",
    [
        ("default", None),
        ("default", "default"),
        ("auto", "default"),
        ("auto", None),
        ("default", "auto"),
    ],
)
async def test_a_missing_pair_prompt_never_refuses_the_start(
    minimal_settings, source_level, selected_level
):
    from gc_testkit.memory_agent_store import MemoryAgentStore

    cfg = minimal_settings.with_overrides(
        {
            "models": {"fast": {}, "mid": {}, "big": {}},
            "slots": {"light": "fast", "default": "mid", "max": "big"},
            "backend.api_mode": "chat_completions",
            "level": source_level,
            "paths.chat_completions_routing_pair_judge_prompt_path": "/virtual/missing-pair-prompt.md",
        }
    ).settings
    store = MemoryAgentStore()
    kwargs = {"agent_store": store}
    if selected_level is not None:
        kwargs["choice_provider"] = lambda: selected_level
    ctx = await rc.acreate_run_context(cfg, **kwargs)
    assert ctx.model_router.live_choice() == (selected_level or source_level)
    assert ("prompt", "routing_pair_judge") not in store.reads
