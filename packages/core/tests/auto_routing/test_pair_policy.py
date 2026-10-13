"""Уровень хода по оценке судьи пары и резолвер `safe_level: auto` (спека pair-judge §2)."""
import pytest

from gc_core.config_modules.root import AppConfig

from gc_core.auto_routing.policy import (
    DEFAULT_PAIR_THRESHOLD, RoutingParams, SAFE_LEVEL_AUTO, pair_levels,
    pair_safe_level, turn_params,
)


@pytest.mark.parametrize("score, threshold, level", [
    (1, 4, "default"), (3, 4, "default"), (4, 4, "max"), (5, 4, "max"),
    (1, 2, "default"), (2, 2, "max"), (4, 5, "default"), (5, 5, "max"),
])
def test_the_score_against_the_threshold_picks_the_turn_level(score, threshold, level):
    assert pair_safe_level(score, threshold) == level


def test_auto_takes_the_level_chosen_for_the_turn():
    params = RoutingParams(safe_level=SAFE_LEVEL_AUTO)
    assert turn_params(params, "default").safe_level == "default"
    assert pair_levels(turn_params(params, "default")) == ("light", "default")
    assert pair_levels(turn_params(params, "max")) == ("default", "max")


def test_auto_without_a_chosen_pair_is_the_hard_pair():
    assert turn_params(RoutingParams(safe_level=SAFE_LEVEL_AUTO), None).safe_level == "max"


@pytest.mark.parametrize("safe_level", ["default", "max"])
def test_a_fixed_safe_level_is_left_alone(safe_level):
    params = RoutingParams(safe_level=safe_level)
    assert turn_params(params, "default") is params
    assert turn_params(params, None) is params


def test_the_resolver_uses_the_hard_pair_before_the_turn_pair_is_selected():
    params = RoutingParams(safe_level=SAFE_LEVEL_AUTO)
    assert turn_params(params, None).safe_level == "max"


def test_the_threshold_and_auto_come_from_typed_settings():
    assert RoutingParams.from_config(AppConfig.model_validate({"agent": {"auto_routing_two_level": {"pair_threshold": 3}}})).pair_threshold == 3
    assert RoutingParams.from_config(AppConfig.model_validate({"agent": {}})).pair_threshold == DEFAULT_PAIR_THRESHOLD == 3
    assert RoutingParams.from_config(AppConfig.model_validate({"agent": {"auto_routing_safe_level": "auto"}})).safe_level == "auto"
