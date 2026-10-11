"""agent.model_steering → special_instructions merge."""

import pytest
from gc_core.config_modules.agent import (
    AgentConfig,
    merge_special_instructions_with_model_steering,
    resolve_model_steering_text,
)
from gc_core.utils.local_render_system_prompt import render_system_prompt
from gc_testkit.settings import make_settings
from pydantic import ValidationError


def test_agent_config_accepts_model_steering_string():
    cfg = AgentConfig(model_steering="  Prefer act over survey.  ")
    assert cfg.model_steering == "Prefer act over survey."


def test_agent_config_rejects_non_string_steering():
    with pytest.raises(ValidationError):
        AgentConfig(model_steering={"m": "x"})


def test_resolve_model_steering_text():
    assert (
        resolve_model_steering_text(
            make_settings({"agent.model_steering": "Cap exploratory reads."})
        )
        == "Cap exploratory reads."
    )


def test_resolve_model_steering_empty_is_none():
    assert (
        resolve_model_steering_text(
            make_settings({"agent.model_steering": ""})
        )
        is None
    )
    assert resolve_model_steering_text(make_settings({})) is None


def test_merge_appends_steering_after_base():
    out = merge_special_instructions_with_model_steering(
        "TUI rules",
        settings=make_settings({"agent.model_steering": "Prefer act."}),
    )
    assert out is not None
    assert out.startswith("TUI rules")
    assert "### Model steering\nPrefer act." in out


def test_merge_steering_only():
    out = merge_special_instructions_with_model_steering(
        None,
        settings=make_settings({"agent.model_steering": "Act."}),
    )
    assert out == "### Model steering\nAct."


def test_merge_none_when_empty():
    assert (
        merge_special_instructions_with_model_steering(
            None, settings=make_settings({})
        )
        is None
    )
    assert (
        merge_special_instructions_with_model_steering(
            "  ", settings=make_settings({})
        )
        is None
    )


def test_render_injects_merged_steering_into_slot():
    merged = merge_special_instructions_with_model_steering(
        "BASE",
        settings=make_settings({"agent.model_steering": "Prefer act."}),
    )
    tmpl = "H\n<special_instructions></special_instructions>\nF"
    out = render_system_prompt(
        tmpl,
        tools_spec=[],
        project_info={},
        specific_role=None,
        special_instructions=merged,
    )
    assert (
        "<special_instructions>BASE\n\n### Model steering\nPrefer act.</special_instructions>"
        in out
    )
