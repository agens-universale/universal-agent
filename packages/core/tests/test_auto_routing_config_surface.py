"""Продуктовый конфиг автовыбора: параметры пары и валидация настроек."""
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from gc_core.auto_routing.levels import resolve_routing_params
from gc_core.auto_routing.policy import DEFAULT_PAIR_THRESHOLD, RoutingParams, pair_levels, pair_start, turn_params
from gc_core.config import _default_config_path, apply_cli_overrides
from gc_core.config_modules.root import AppConfig
from gc_core.settings import LoadedSettings
from gc_testkit.paths import REPO
from gc_testkit.settings import fake_ctx


def test_schema_has_only_the_product_routing_settings():
    cfg = AppConfig()
    assert cfg.agent.auto_routing is True
    assert cfg.agent.auto_routing_safe_level == "auto"
    assert cfg.agent.auto_routing_two_level.model_dump() == {
        "weak_steps": 10, "strong_steps": 2, "pair_threshold": 3,
    }
    assert {key for key in type(cfg.agent).model_fields if key.startswith("auto_routing")} == {
        "auto_routing", "auto_routing_safe_level", "auto_routing_two_level",
    }


def test_shipped_templates_carry_product_defaults_and_bindings():
    for path in (Path(_default_config_path()), REPO / "demo_config.yml"):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert raw["agent"]["auto_routing"] is True
        assert raw["agent"]["auto_routing_safe_level"] == "auto"
        assert raw["agent"]["auto_routing_two_level"] == {
            "weak_steps": 10, "strong_steps": 2, "pair_threshold": 3,
        }
        assert {key for key in raw["agent"] if key.startswith("auto_routing")} == {
            "auto_routing", "auto_routing_safe_level", "auto_routing_two_level",
        }
        assert raw["assign"] == {
            "command_judge": "default", "finish_gate": "max",
            "routing_judge": "default", "summary": "default",
        }


@pytest.mark.parametrize("value", ["default", "max", "auto"])
def test_safe_level_accepts_the_pair_targets(value):
    assert AppConfig.model_validate({"agent": {"auto_routing_safe_level": value}}).agent.auto_routing_safe_level == value


@pytest.mark.parametrize("value", ["light", "", None, True, "Default"])
def test_invalid_safe_levels_are_refused(value):
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"agent": {"auto_routing_safe_level": value}})


@pytest.mark.parametrize("key", ["weak_steps", "strong_steps"])
@pytest.mark.parametrize("value", [0, -1, True, "3", 2.5, None])
def test_steps_must_be_positive_integers(key, value):
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"agent": {"auto_routing_two_level": {key: value}}})


@pytest.mark.parametrize("value", [1, 6, True, "4", 3.5, None])
def test_pair_threshold_must_be_an_integer_from_two_to_five(value):
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"agent": {"auto_routing_two_level": {"pair_threshold": value}}})


def test_partial_two_level_settings_keep_other_defaults_and_reject_unknown_keys():
    cfg = AppConfig.model_validate({"agent": {"auto_routing_two_level": {"strong_steps": 4}}})
    assert cfg.agent.auto_routing_two_level.model_dump() == {
        "weak_steps": 10, "strong_steps": 4, "pair_threshold": 3,
    }
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"agent": {"auto_routing_two_level": {"steps": 3}}})


def test_typed_sources_reach_the_params_and_flat_dicts_are_rejected():
    cfg = AppConfig.model_validate({"agent": {
        "auto_routing_two_level": {"weak_steps": 7, "strong_steps": 3, "pair_threshold": 4},
        "auto_routing_safe_level": "max",
    }})
    loaded = LoadedSettings(settings=cfg)
    for source in (cfg, loaded, fake_ctx(loaded=loaded)):
        params = RoutingParams.from_config(source)
        assert (params.safe_level, params.weak_steps, params.strong_steps, params.pair_threshold) == ("max", 7, 3, 4)
    with pytest.raises(TypeError):
        RoutingParams.from_config({"auto_routing_safe_level": "max"})


def test_unresolved_schema_auto_and_resolved_low_level_default_are_distinct():
    assert RoutingParams.from_config(AppConfig()).safe_level == "auto"
    assert RoutingParams() == resolve_routing_params(AppConfig())


@pytest.mark.parametrize("field", ["weak_steps", "strong_steps", "pair_threshold"])
def test_nested_two_level_settings_are_immutable(field):
    with pytest.raises(ValidationError, match="frozen_instance"):
        setattr(AppConfig().agent.auto_routing_two_level, field, 2)


def test_cli_overrides_validate_typed_pair_settings():
    cfg = apply_cli_overrides(LoadedSettings(settings=AppConfig()), extra_overrides={
        "agent.auto_routing_safe_level": "max",
        "agent.auto_routing_two_level.strong_steps": 4,
    })
    assert cfg.settings.agent.auto_routing_safe_level == "max"
    assert cfg.settings.agent.auto_routing_two_level.strong_steps == 4
    with pytest.raises(ValidationError):
        apply_cli_overrides(cfg, extra_overrides={"agent.auto_routing_two_level.strong_steps": 0})


def test_named_routing_judge_binding_resolves_and_validates():
    from gc_core.config_modules.model_sections_validation import validate_model_sections
    from gc_core.model_router import ModelRouter

    cfg = AppConfig.model_validate({
        "models": {"main-model": {}, "judge-model": {"model": "judge"}},
        "slots": {"default": "main-model"},
        "assign": {"routing_judge": "judge-model"},
    })
    assert validate_model_sections(cfg) == []
    router = ModelRouter(cfg, level_provider=lambda: "max")
    assert router.resolve("routing_judge").alias == "judge-model"
    with pytest.raises(ValueError, match="assign.routing_judge"):
        validate_model_sections(AppConfig.model_validate({"assign": {"routing_judge": "missing"}}))


def test_registry_write_preserves_the_judge_binding(tmp_path):
    from gc_core.config_modules.registry_write import write_assign

    target = tmp_path / "config.yml"
    target.write_text("models:\n  m: {}\nslots:\n  default: m\n", encoding="utf-8")
    write_assign(target, "routing_judge", "default", template="")
    assert "routing_judge:" in target.read_text(encoding="utf-8")


@pytest.mark.parametrize("score,levels", [(2, ("light", "default")), (3, ("default", "max"))])
def test_product_pair_threshold_selects_the_pair_at_score_three(score, levels):
    params = RoutingParams.from_config(AppConfig())
    assert params.pair_threshold == DEFAULT_PAIR_THRESHOLD == 3
    pair = pair_start(score, params)
    assert pair_levels(turn_params(params, pair.safe_level)) == levels
