"""RunContext: ModelRouter + level_provider + фиксация модели на ход (begin_turn).

См. .superpowers/sdd/2026-07-27-profiles-wave-a-plan/task-5-brief.md.
"""
from types import SimpleNamespace

import pytest
from gc_core import run_context as rc
from gc_core.config import flatten_app_config
from gc_core.config_modules.root import AppConfig
from gc_core.model_router import ModelRouter
from gc_core.states import State
from gc_core.turn_boundary import begin_turn
from gc_testkit import as_coro


@pytest.fixture
def minimal_config_dict(monkeypatch):
    monkeypatch.setattr(
        rc,
        "abuild_tools_data",
        as_coro(lambda cfg, on_progress=None, **_kwargs: ({}, [], [], {})),
    )
    return {"model": "M", "level": "max"}


def _snapshot(raw: dict) -> dict:
    """Реальный снимок config_dict — через настоящий flatten_app_config, не мок."""
    return flatten_app_config(AppConfig.model_validate(raw))


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


# --- create_run_context: model_router + level_provider ---------------------------


async def test_create_run_context_without_level_provider_reads_config_level(minimal_config_dict):
    ctx = await rc.acreate_run_context(minimal_config_dict)
    assert isinstance(ctx.model_router, ModelRouter)
    assert ctx.model_router.active_level() == "max"


async def test_create_run_context_with_level_provider_uses_callable(minimal_config_dict):
    level = {"value": "light"}
    ctx = await rc.acreate_run_context(minimal_config_dict, level_provider=lambda: level["value"])
    assert ctx.model_router.active_level() == "light"
    level["value"] = "default"
    assert ctx.model_router.active_level() == "default"  # провайдер живой, не закэширован


async def test_create_run_context_model_router_resolves_legacy_main(minimal_config_dict):
    ctx = await rc.acreate_run_context(minimal_config_dict)
    resolved = ctx.model_router.resolve("main")
    assert resolved.model == "M"


async def test_create_run_context_force_legacy_propagates_to_router(minimal_config_dict):
    """Task 10: `--model` при непустом реестре -> `force_legacy=True` доезжает от
    CLI до ModelRouter через create_run_context (спека §2)."""
    ctx = await rc.acreate_run_context(minimal_config_dict, force_legacy=True)
    resolved = ctx.model_router.resolve("main")
    assert resolved.model == "M"
    assert resolved.source == "легаси-форс --model"


async def test_create_run_context_default_force_legacy_is_false(minimal_config_dict):
    ctx = await rc.acreate_run_context(minimal_config_dict)
    resolved = ctx.model_router.resolve("main")
    assert resolved.source == "легаси"


# --- begin_turn: юнит + фиксация на ход -------------------------------------------


def test_begin_turn_sets_default_model_from_router_resolve_main(minimal_config_dict):
    router = ModelRouter(minimal_config_dict, level_provider=lambda: "max")
    ctx = SimpleNamespace(model_router=router)
    state = _make_state()

    begin_turn(state, ctx)

    assert state.default_model == "M"


def test_begin_turn_is_noop_without_model_router():
    """Легаси-совместимость: ctx без model_router (тестовые двойники) не должен падать —
    default_model остаётся тем, что уже стоит на state."""
    ctx = SimpleNamespace(config_dict={"model": "X"})
    state = _make_state()

    begin_turn(state, ctx)

    assert state.default_model == "unused-legacy-default"


def test_begin_turn_refixes_model_between_turns_when_level_changes():
    """Спека §6.4: смена активного уровня меняет default_model следующего хода —
    только через явный begin_turn, не сама по себе внутри текущего хода."""
    snapshot = _snapshot({
        "models": {
            "fast": {"url": "http://fast/v1", "token": ""},
            "smart": {"url": "http://smart/v1", "token": ""},
        },
        "slots": {"light": "fast", "max": "smart"},
    })
    level = {"value": "light"}
    router = ModelRouter(snapshot, level_provider=lambda: level["value"])
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


# --- пин уровня хода: транспортный резолв main -------------------------------------


def _two_slot_router(level_box: dict) -> ModelRouter:
    snapshot = _snapshot({
        "models": {
            "fast": {"url": "http://fast/v1", "token": ""},
            "smart": {"url": "http://smart/v1", "token": ""},
        },
        "slots": {"light": "fast", "max": "smart"},
    })
    return ModelRouter(snapshot, level_provider=lambda: level_box["value"])


def test_main_transport_resolve_is_pinned_to_turn_level():
    """Контракт §6.4 в ТРАНСПОРТЕ: воронка получает весь `ResolvedModel`
    (url/токен/квирки), а не только `state.default_model`, — поэтому смена `/level`
    посреди хода не должна уводить запрос на другой слот. Новый ход (`begin_turn`)
    подхватывает актуальный уровень."""
    from gc_core import processing_responses as pr

    level = {"value": "light"}
    ctx = SimpleNamespace(model_router=_two_slot_router(level))
    state = _make_state()

    begin_turn(state, ctx)
    assert pr._resolve_main_for_turn(state, ctx).alias == "fast"

    level["value"] = "max"                     # живой /level посреди хода
    assert pr._resolve_main_for_turn(state, ctx).alias == "fast"
    assert pr._resolve_main_for_turn(state, ctx).url == "http://fast/v1"

    begin_turn(state, ctx)                     # следующий ход
    assert pr._resolve_main_for_turn(state, ctx).alias == "smart"


def test_turn_level_is_declared_on_state_before_any_turn():
    """`turn_level` — служебное поле прогона, значит объявлено в State наравне с
    `turn_tool_calls`. Три читателя берут его через `getattr(..., None)`, и без
    объявления «ход ещё не начинали» неотличимо от «поля нет вовсе»: транспорт
    молча уходил бы на живой резолв — ровно тот дрейф /level посреди хода, ради
    которого пин и заведён."""
    state = _make_state()
    assert hasattr(state, "turn_level")
    assert state.turn_level is None

    level = {"value": "light"}
    ctx = SimpleNamespace(model_router=_two_slot_router(level))
    begin_turn(state, ctx)
    assert state.turn_level == "light"


def test_pinned_main_resolve_still_checks_circuit_breaker():
    """Пин фиксирует ВЫБОР модели, а не её здоровье: алиас, открывшийся посреди
    хода, обязан отказать резолву так же, как при живом `resolve("main")`."""
    from gc_core import processing_responses as pr
    from gc_core.model_router import AliasCircuitOpenError

    level = {"value": "light"}
    router = _two_slot_router(level)
    ctx = SimpleNamespace(model_router=router)
    state = _make_state()
    begin_turn(state, ctx)

    router.breaker.trip("fast", "5xx после ретраев")
    with pytest.raises(AliasCircuitOpenError):
        pr._resolve_main_for_turn(state, ctx)


def test_pinned_main_resolve_keeps_subagent_override():
    """У дочернего Router'а субагента уровень уже заморожен внутри самого Router'а,
    а привязка живёт в `unbound_override` — пин уровня родителя его подменять не
    должен (иначе субагент с пином ушёл бы на слот активного уровня)."""
    from gc_core import processing_responses as pr

    level = {"value": "light"}
    parent = _two_slot_router(level)
    child = parent.with_override(parent.resolve_level("max"))
    state = _make_state()
    begin_turn(state, SimpleNamespace(model_router=parent))
    assert state.turn_level == "light"

    resolved = pr._resolve_main_for_turn(state, SimpleNamespace(model_router=child))
    assert resolved.alias == "smart"
