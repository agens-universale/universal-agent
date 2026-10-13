"""RoutingParams: сборка из конфига, валидация схемы, проводка на оба RunContext."""
import pytest
from pydantic import ValidationError

from gc_core import processing_responses as pr
from gc_core import run_context as rc
from gc_core.auto_routing.levels import resolve_routing_params
from gc_core.auto_routing.policy import RoutingParams
from gc_core.config_modules.root import AppConfig
from gc_core.model_router import ModelRouter
from gc_testkit.settings import make_settings
from gc_core.states import SharedStateData, State
from gc_core.tools.subagents.runner import SubagentRunner
from gc_testkit import run_coro
from gc_testkit.helpers.tool_ctx import local_ctx

RAW = {
    "models": {"fast": {}, "mid": {}, "big": {}},
    "slots": {"light": "fast", "default": "mid", "max": "big"},
    "backend": {"api_mode": "chat_completions"},
    "level": "auto",
    "logging": {"save_logs": False},
}


def _settings(**agent_over) -> AppConfig:
    raw = {**RAW}
    if agent_over:
        raw["agent"] = agent_over
    return AppConfig.model_validate(raw)


# --- from_config -------------------------------------------------------------

def test_from_config_defaults_are_product_behaviour():
    params = RoutingParams.from_config(_settings())
    assert params.safe_level == "auto"          # сырое значение схемы; разрешает его resolve_routing_params


def test_bare_routing_params_matches_schema_defaults():
    """Страж дрейфа: дефолты `RoutingParams` объявлены и в policy.py, и в схеме
    (`AgentConfig` / `AutoRoutingTwoLevelConfig`). Голый `RoutingParams()` —
    реальный рабочий объект (`default_factory` у `RunContext.routing_params`,
    фолбэк `_routing_params`), так что он обязан совпадать с тем, что рождает
    схема и разрешает `resolve_routing_params` (рантайм получает параметры только
    разрешёнными: без трёх моделей `safe_level: auto` — это `default`)."""
    assert RoutingParams() == resolve_routing_params(AppConfig())


# --- валидация схемы ---------------------------------------------------------


@pytest.mark.parametrize("value", ["light", "", None, "Default"])
def test_safe_level_outside_default_and_max_is_refused(value):
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"agent": {"auto_routing_safe_level": value}})


# --- проводка на RunContext --------------------------------------------------


def _child_config(tmp_path, **agent_over):
    return make_settings({
        **RAW,
        "agent": {
            **agent_over,
            "use_local_subagents": True,
            "subagent_max_steps": 30,
            "system_prompt_id": "test-prompt",
        },
        "paths.skills_data_path": "",
        "paths.logs_dump_dir": str(tmp_path),
        "logging.save_logs": True,
        "mcp.servers": {},
    })


def _parent_ctx(cfg, *, level: str):
    ctx = local_ctx(cfg)
    ctx.run_id = "parent_run"
    ctx.auth_connector = None
    ctx.logger = None
    ctx.logging_path = ""
    ctx.summary_trace_path = ""
    ctx.tools_data = {}
    ctx.tools_spec = []
    ctx.tools_description = []
    ctx.is_tui = False
    ctx.model_router = ModelRouter(cfg, level_provider=lambda: level)
    return ctx


def _orchestrator_state() -> State:
    state = State(
        verbose=False, model="test-model", tools_spec=[{"name": "fs_read_file"}],
        tools_descr=["read desc"], skills_list=[], max_steps=100,
        system_prompt_id="test-prompt", use_server_subagents=False,
        chat_completions_system_prompt="System", shared=SharedStateData(),
    )
    state.backend_api_mode = "chat_completions"
    return state


def test_unbound_child_under_two_level_starts_its_turn_on_the_strong_level(tmp_path):
    """Ребёнок ведёт свой счёт той же политикой: первое решение его хода — сильная
    модель пары (`max` при `auto_routing_safe_level: max`), а не `default`."""
    cfg = _child_config(tmp_path, auto_routing_safe_level="max")
    runner = SubagentRunner(_parent_ctx(cfg, level="auto"), _orchestrator_state(), cfg)
    routed = []

    async def fake_run_agent(messages, child_state, child_ctx, **kwargs):
        routed.append((await pr.aroute_before_call(child_state, child_ctx))[2])
        return "ok"

    run_coro(runner.arun("subagent_planner", {"task": "plan it", "reason": "r"}, fake_run_agent))

    assert routed == ["max"]


def test_two_level_numbers_reach_routing_params_through_typed_settings():
    """Числа из схемы доезжают до политики без отдельной проекции."""
    params = RoutingParams.from_config(_settings(
        auto_routing_two_level={"weak_steps": 5, "strong_steps": 2},
    ))
    assert (params.policy, params.weak_steps, params.strong_steps) == ("two_level", 5, 2)


def test_unbound_child_ctx_inherits_the_two_level_numbers(tmp_path):
    """Непривязанный субагент ведёт свой счёт той же политикой и теми же числами."""
    cfg = _child_config(
        tmp_path,
        auto_routing_safe_level="max",
        auto_routing_two_level={"weak_steps": 5, "strong_steps": 2},
    )
    runner = SubagentRunner(_parent_ctx(cfg, level="auto"), _orchestrator_state(), cfg)

    child_ctx = run_coro(runner._build_child_ctx(tool_name="subagent_worker"))   # без привязки

    params = child_ctx.routing_params
    assert (params.policy, params.safe_level) == ("two_level", "max")
    assert (params.weak_steps, params.strong_steps) == (5, 2)


def test_routing_params_contains_the_pair_settings():
    assert set(vars(RoutingParams())) == {
        "safe_level", "weak_steps", "strong_steps", "pair_threshold",
    }


def test_legacy_child_gets_the_pair_of_the_parent_turn(tmp_path):
    cfg = _child_config(tmp_path, auto_routing_safe_level="auto")
    parent = _orchestrator_state()
    parent.turn_safe_level = "default"
    parent_ctx = _parent_ctx(cfg, level="auto")
    parent_ctx.subagent_independent_auto = False
    runner = SubagentRunner(parent_ctx, parent, cfg)
    child_ctx = run_coro(runner._build_child_ctx(tool_name="subagent_worker"))
    assert child_ctx.routing_params.safe_level == "default"


def test_legacy_child_without_a_parent_pair_takes_the_hard_pair(tmp_path):
    cfg = _child_config(tmp_path, auto_routing_safe_level="auto")
    parent_ctx = _parent_ctx(cfg, level="auto")
    parent_ctx.subagent_independent_auto = False
    runner = SubagentRunner(parent_ctx, _orchestrator_state(), cfg)
    child_ctx = run_coro(runner._build_child_ctx(tool_name="subagent_worker"))
    assert child_ctx.routing_params.safe_level == "max"


@pytest.mark.parametrize("parent_pair", [None, "default", "max"])
def test_child_keeps_auto_params_instead_of_parent_pair(tmp_path, parent_pair):
    cfg = _child_config(tmp_path, auto_routing_safe_level="auto")
    parent = _orchestrator_state()
    parent.turn_safe_level = parent_pair
    parent.pair_window_left = 7
    runner = SubagentRunner(_parent_ctx(cfg, level="auto"), parent, cfg)
    child = run_coro(runner._build_child_ctx(tool_name="subagent_worker"))
    assert child.routing_params.safe_level == "auto"
    assert parent.turn_safe_level == parent_pair
    assert parent.pair_window_left == 7
