import pytest

from gc_testkit.settings import make_settings

from gc_core.auto_routing.levels import (
    auto_available, auto_unavailable_reason, effective_level, resolve_routing_params,
)

_TWO_KITS = {"models": {"fast": {}, "mid": {}}, "slots": {"light": "fast", "default": "mid"}}


def test_auto_available_requires_chat_completions_flag_and_no_force_legacy():
    base = {"backend.api_mode": "chat_completions", **_TWO_KITS}
    assert auto_available(make_settings(base), force_legacy=False) is True
    assert auto_available(make_settings({**base, "agent.auto_routing": False}), force_legacy=False) is False
    assert auto_available(make_settings({**base, "backend.api_mode": "responses"}), force_legacy=False) is False
    assert auto_available(make_settings(base), force_legacy=True) is False


@pytest.mark.parametrize("registry", [
    {},                                                               # легаси: реестра нет
    {"models": {"only": {}}, "slots": {"default": "only"}},           # одна модель (форма онбординга)
    {"models": {"only": {}}},                                         # одна модель без слотов
    {"models": {"mid": {}}, "slots": {"light": "mid", "default": "mid", "max": "mid"}},
], ids=["legacy", "one_model", "one_model_no_slots", "one_alias_everywhere"])
def test_levels_resolving_to_one_call_kit_make_auto_unavailable(registry):
    cfg = {"backend.api_mode": "chat_completions", **registry}
    assert auto_unavailable_reason(make_settings(cfg), force_legacy=False) == "во всех уровнях одна модель"
    assert auto_available(make_settings(cfg), force_legacy=False) is False


@pytest.mark.parametrize("registry", [
    {"models": {"mid": {}, "big": {}}, "slots": {"default": "mid", "max": "big"}},   # light не задан
    {"models": {"cold": {"model": "m-1", "main": {"temperature": 0.2}},
                "hot": {"model": "m-1", "main": {"temperature": 0.9}}},
     "slots": {"light": "cold", "default": "hot"}},                                     # одна модель, разные параметры
], ids=["light_unset", "same_model_other_params"])
def test_two_distinct_call_kits_keep_auto_available(registry):
    """Два разных комплекта оставляют автовыбор доступным: schema auto выбирает
    ту пару, в которой модели различаются."""
    from gc_core.config_modules.root import AppConfig
    cfg = AppConfig.model_validate(
        {"backend": {"api_mode": "chat_completions"},
         **registry})
    assert auto_unavailable_reason(cfg, force_legacy=False) is None


def test_reason_names_the_first_unmet_condition():
    base = {"backend.api_mode": "chat_completions", **_TWO_KITS}
    assert auto_unavailable_reason(make_settings(base), force_legacy=True) == "уровни отключены флагом --model"
    assert auto_unavailable_reason(make_settings({**base, "agent.auto_routing": False}), force_legacy=False) == (
        "выключен настройкой auto_routing")
    assert auto_unavailable_reason(make_settings({**base, "backend.api_mode": "responses"}), force_legacy=False) == (
        "режим API responses")


def test_effective_level_translates_auto_only():
    assert effective_level("max", None, True) == "max"
    assert effective_level("max", "light", True) == "max"          # ручной игнорирует current
    assert effective_level("auto", None, True) == "default"
    assert effective_level("auto", "light", True) == "light"
    assert effective_level("auto", "light", False) == "default"     # недоступно → default


def test_auto_active_needs_router_raw_auto_availability_and_permission():
    from types import SimpleNamespace
    from gc_core.auto_routing.levels import auto_active
    unbound, bound = SimpleNamespace(auto_allowed=True), SimpleNamespace(auto_allowed=False)
    assert auto_active(unbound, "auto", True) is True
    assert auto_active(unbound, "default", True) is False           # ручной уровень
    assert auto_active(unbound, "auto", False) is False             # auto недоступен конфигу
    assert auto_active(bound, "auto", True) is False                # привязан или деградировал
    assert auto_active(None, "auto", True) is False                 # Router'а нет


# --- политика two_level: сравнивается пара, а не все три уровня ------------------

_TRIO = {"models": {"fast": {}, "mid": {}, "big": {}},
         "slots": {"light": "fast", "default": "mid", "max": "big"}}
_NO_LIGHT = {"models": {"mid": {}, "big": {}}, "slots": {"default": "mid", "max": "big"}}
_NO_MAX = {"models": {"fast": {}, "mid": {}}, "slots": {"light": "fast", "default": "mid"}}
_MAX_IS_DEFAULT = {"models": {"fast": {}, "mid": {}},
                   "slots": {"light": "fast", "default": "mid", "max": "mid"}}


def _two_level(registry, safe_level):
    return {"backend.api_mode": "chat_completions",
            "agent.auto_routing_safe_level": safe_level, **registry}


@pytest.mark.parametrize("registry, safe_level, reason", [
    (_TRIO, "default", None),
    (_TRIO, "max", None),
    # слот light не задан — его подменяет default: в дефолтной паре одна модель
    (_NO_LIGHT, "default",
     "в паре light+default одна модель; другая пара — auto_routing_safe_level: max"),
    (_NO_LIGHT, "max", None),
    # зеркально: слот max не задан — его подменяет default
    (_NO_MAX, "max",
     "в паре default+max одна модель; другая пара — auto_routing_safe_level: default"),
    (_NO_MAX, "default", None),
    # третий уровень под политикой не используется: совпал он с соседом или нет — неважно
    (_MAX_IS_DEFAULT, "default", None),
    (_MAX_IS_DEFAULT, "max",
     "в паре default+max одна модель; другая пара — auto_routing_safe_level: default"),
], ids=["trio_low", "trio_high", "no_light_low", "no_light_high", "no_max_high", "no_max_low",
        "max_is_default_low", "max_is_default_high"])
def test_two_level_compares_the_call_kits_of_the_pair(registry, safe_level, reason):
    cfg = _two_level(registry, safe_level)
    assert auto_unavailable_reason(make_settings(cfg), force_legacy=False) == reason
    assert auto_available(make_settings(cfg), force_legacy=False) is (reason is None)


@pytest.mark.parametrize("safe_level", ["default", "max"])
@pytest.mark.parametrize("registry", [
    {},
    {"models": {"only": {}}, "slots": {"default": "only"}},
    {"models": {"mid": {}}, "slots": {"light": "mid", "default": "mid", "max": "mid"}},
], ids=["legacy", "one_model", "one_alias_everywhere"])
def test_one_model_everywhere_keeps_the_common_reason_with_pair_routing(registry, safe_level):
    """Обе пары вырождены — значит, модель одна на все уровни: текст тот же, что у
    остальных политик, подсказывать другую пару незачем."""
    assert auto_unavailable_reason(
        make_settings(_two_level(registry, safe_level)), force_legacy=False) == "во всех уровнях одна модель"




def test_a_degenerate_pair_gets_no_hint_when_the_other_pair_is_no_better():
    """Битый реестр: слот default указывает на алиас, которого нет. Уровень не
    резолвится и в сравнение не входит, обе пары вырождены — подсказывать нечего."""
    broken = {"models": {"fast": {}, "big": {}},
              "slots": {"light": "fast", "default": "ghost", "max": "big"}}
    assert auto_unavailable_reason(make_settings(_two_level(broken, "default")), force_legacy=False) == (
        "в паре light+default одна модель")
    assert auto_unavailable_reason(make_settings(_two_level(broken, "max")), force_legacy=False) == (
        "в паре default+max одна модель")


def test_levels_module_imports_only_the_pure_policy_at_module_level():
    """`levels` грузится рано (его тянет пакет `gc_core.tools`): на уровне модуля —
    только чистый `policy`; роутер моделей импортируется лениво, внутри функции."""
    import ast
    import inspect

    from gc_core.auto_routing import levels
    tree = ast.parse(inspect.getsource(levels))
    imported = {node.module for node in tree.body if isinstance(node, ast.ImportFrom)}
    assert {name for name in imported if name.startswith("gc_core")} == {
        "gc_core.auto_routing.policy"}


_AUTO = {"backend.api_mode": "chat_completions",
         "agent.auto_routing_safe_level": "auto"}


@pytest.mark.parametrize("registry, available", [
    ({"models": {"only": {}}, "slots": {"default": "only"}}, False),
    ({"models": {"fast": {}, "mid": {}}, "slots": {"light": "fast", "default": "mid"}}, True),
    ({"models": {"mid": {}, "big": {}}, "slots": {"default": "mid", "max": "big"}}, True),
    ({"models": {"fast": {}, "mid": {}, "big": {}},
      "slots": {"light": "fast", "default": "mid", "max": "big"}}, True),
], ids=["one_model", "two_models_no_max", "two_models_no_light", "three_models"])
def test_auto_safe_level_needs_only_two_distinct_models(registry, available):
    assert auto_available(make_settings({**_AUTO, **registry}), force_legacy=False) is available


# --- safe_level: auto — лестница пар только когда в обеих парах две разные модели ----------

_LAYOUT_B = {"models": {"fast": {}, "big": {}},
             "slots": {"light": "fast", "default": "fast", "max": "big"}}


@pytest.mark.parametrize("registry, resolved", [
    (_TRIO, "auto"),                 # три модели — лестница пар
    (_NO_MAX, "default"),            # верхняя пара вырождена — ss2 на light+default
    (_MAX_IS_DEFAULT, "default"),
    (_NO_LIGHT, "max"),              # нижняя пара вырождена — ss2 на default+max
    (_LAYOUT_B, "max"),
    ({"models": {"only": {}}, "slots": {"default": "only"}}, "default"),   # одна модель: auto и так недоступен
], ids=["trio", "no_max", "max_is_default", "no_light", "layout_b", "one_model"])
def test_auto_safe_level_runs_the_ladder_only_when_both_pairs_have_two_models(registry, resolved):
    assert resolve_routing_params(make_settings(_two_level(registry, "auto"))).safe_level == resolved




@pytest.mark.parametrize("safe_level", ["default", "max"])
def test_a_fixed_safe_level_is_kept_as_is(safe_level):
    assert resolve_routing_params(make_settings(_two_level(_NO_MAX, safe_level))).safe_level == safe_level


@pytest.mark.parametrize("copies", [2, 3])
def test_identical_requests_under_different_aliases_disable_auto(copies):
    aliases = ["a", "b", "c"][:copies]
    entry = {"model": "same", "url": "http://local/v1/chat/completions", "token": "",
             "main": {"temperature": 0.2}}
    cfg = make_settings({"models": {alias: entry for alias in aliases},
        "slots": {"light": "a", "default": "b", "max": aliases[-1]},
        "backend.api_mode": "chat_completions"})
    assert auto_unavailable_reason(cfg, force_legacy=False) == "во всех уровнях одна модель"


@pytest.mark.parametrize("overlay", [
    {"auxiliary": {"temperature": 0.9}},
    {"summary_trigger_chars": 12345},
    {"strip_think": False},
])
def test_non_main_metadata_does_not_enable_auto(overlay):
    entry = {"model": "same", "main": {"temperature": 0.2}}
    cfg = make_settings({"models": {"a": entry, "b": {**entry, **overlay}},
        "slots": {"light": "a", "default": "b"}, "backend.api_mode": "chat_completions"})
    assert auto_available(cfg, force_legacy=False) is False


def test_inherited_and_explicit_equal_main_kwargs_are_one_request():
    cfg = make_settings({"models": {"a": {"model": "same"},
        "b": {"model": "same", "main": {"temperature": 0.2}}},
        "backend.chat_completion_kwargs": {"temperature": 0.2},
        "slots": {"light": "a", "default": "b"}, "backend.api_mode": "chat_completions"})
    assert auto_available(cfg, force_legacy=False) is False


def test_old_durable_contract_retains_alias_identity():
    cfg = make_settings({"models": {"a": {"model": "same"}, "b": {"model": "same"}},
        "slots": {"light": "a", "default": "b"}, "backend.api_mode": "chat_completions"})
    assert auto_available(cfg, force_legacy=False, legacy_call_kits=True) is True


@pytest.mark.parametrize("overlay", [
    {"main": {"temperature": 0.9}},
    {"token": "different"},
    {"url": "http://other/v1/chat/completions"},
    {"backend": {"send_tool_strict": False}},
    {"backend": {"chat_completions_max_tokens": 1234}},
    {"backend": {"chat_completion_headers": {"x-backend": "different"}}},
])
def test_actual_main_request_differences_keep_auto_available(overlay):
    entry = {"model": "same", "url": "http://local/v1/chat/completions", "token": "",
             "main": {"temperature": 0.2}}
    cfg = make_settings({"models": {"a": entry, "b": {**entry, **overlay}},
        "slots": {"light": "a", "default": "b"}, "backend.api_mode": "chat_completions"})
    assert auto_available(cfg, force_legacy=False) is True


def test_vision_none_and_true_are_the_same_request():
    cfg = make_settings({"models": {"a": {"model": "same"},
        "b": {"model": "same", "backend": {"vision": True}}},
        "slots": {"light": "a", "default": "b"},
        "backend.api_mode": "chat_completions", "backend.vision": None})
    assert auto_available(cfg, force_legacy=False) is False


def test_child_binding_preserves_old_durable_alias_comparison():
    from types import SimpleNamespace
    from gc_core.model_router import ModelRouter
    from gc_core.tools.subagents.runner import SubagentRunner
    cfg = make_settings({"models": {alias: {"model": "same"} for alias in ("a", "b", "c")},
        "slots": {"light": "a", "default": "b", "max": "c"},
        "backend.api_mode": "chat_completions"})
    child = SimpleNamespace(settings=cfg.settings, runtime=cfg.runtime, routing_contract_v2=False, subagent_independent_auto=False,
                            model_router=ModelRouter(cfg, level_provider=lambda: "auto"))
    runner = object.__new__(SubagentRunner)
    runner._ctx = SimpleNamespace()
    runner._orchestrator_state = SimpleNamespace(turn_safe_level="max")
    runner._parent_call_id = "parent"
    result = runner._bind_child_context(child, "subagent_worker")
    assert result.auto_available is True
    assert result.routing_params.safe_level == "max"
    assert result.model_router.live_level() == "auto"
