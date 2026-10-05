"""Пути настроек: плоских имён прежнего ``config_dict`` нет, подсказка — по
последнему сегменту пути схемы."""

import pytest
from gc_core.config import apply_cli_overrides
from gc_core.config_modules.path_hints import (
    UnknownSettingError,
    is_known_path,
    suggest_paths,
)
from gc_testkit.settings import make_settings


@pytest.mark.parametrize(
    "path",
    [
        "agent.max_steps",
        "agent",
        "security.write_guard.enabled",
        "models",
        "models.fast.token",
        "mcp.servers.github.command",
        "backend.chat_completion_kwargs.temperature",
        "level",
    ],
)
def test_schema_paths_are_known(path):
    assert is_known_path(path)


@pytest.mark.parametrize(
    "name", ["max_steps", "summary_params", "agent.max_stepz", "mcp-servers"]
)
def test_flat_names_and_typos_are_unknown(name):
    assert not is_known_path(name)


def test_suggestions_share_the_last_segment():
    assert suggest_paths("max_steps") == ["agent.max_steps"]
    assert suggest_paths("write_guard") == ["security.write_guard"]
    assert suggest_paths("stream") == ["backend.stream", "tui.stream"]
    assert suggest_paths("summary_params") == []


def test_cli_override_with_flat_name_fails_with_hint():
    with pytest.raises(UnknownSettingError) as exc:
        apply_cli_overrides(make_settings(), extra_overrides={"max_steps": 50})
    assert str(exc.value) == (
        "неизвестная настройка --max_steps (возможно, --agent.max_steps)"
    )


def test_cli_override_without_similar_path_has_no_hint():
    with pytest.raises(UnknownSettingError) as exc:
        apply_cli_overrides(make_settings(), extra_overrides={"nope": 1})
    assert str(exc.value) == "неизвестная настройка --nope"


def test_cli_override_by_path_applies():
    loaded = apply_cli_overrides(
        make_settings(), extra_overrides={"agent.max_steps": 50}
    )
    assert loaded.settings.agent.max_steps == 50
    assert loaded.provenance.source("agent.max_steps").layer == "cli"
