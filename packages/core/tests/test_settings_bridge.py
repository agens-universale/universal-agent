"""Мост переходного периода: потребители читают ``settings_of``/``runtime_of``.

Пока часть кода и тестов живёт на плоском ``config_dict``, мост обязан давать
те же настройки, что и типизированный путь: плоский вид → настройки → плоский
вид — без потерь, включая признаки «задано явно».
"""

from types import SimpleNamespace

import pytest
from gc_core.config import flat_config
from gc_core.settings import (
    LoadedSettings,
    RuntimeEnv,
    default_settings,
    is_explicit,
    override_settings,
    runtime_of,
    settings_from_flat,
    settings_of,
)
from pydantic import ValidationError


def _rich_settings():
    return override_settings(
        default_settings(),
        {
            "agent.model": "m",
            "backend.completions_url": "http://h/v1/chat/completions",
            "models": {
                "flash": {
                    "url": "http://h/v1/chat/completions",
                    "model": "f",
                    "token": "t",
                    "backend": {"use_retry": False},
                },
                "plain": {"model": "p"},
            },
            "slots": {"light": "flash"},
            "mcp.servers": {"x": {"transport": "stdio", "command": "echo"}},
            "tui.finish_gate_mode": "on",
        },
    )


@pytest.mark.parametrize("settings", [default_settings(), _rich_settings()])
def test_flat_view_round_trip_is_lossless(settings):
    loaded = LoadedSettings(
        settings=settings, runtime=RuntimeEnv(launch_root="/w")
    )
    flat = flat_config(loaded)
    assert dict(flat_config(settings_from_flat(flat))) == dict(flat)


def test_round_trip_keeps_implicit_defaults_implicit():
    # Плоский вид всегда несёт `stream` и `tui_finish_gate_mode`; обратный путь
    # не имеет права сделать их явными — иначе ложное предупреждение об
    # устаревшем ключе и другое правило /gate.
    back = settings_from_flat(flat_config(default_settings())).settings
    assert not is_explicit(back, "backend.stream")
    assert not is_explicit(back, "tui.finish_gate_mode")


def test_settings_of_reads_every_source_the_same_way():
    loaded = settings_from_flat({"max_steps": 3, "launch_root": "/r"})
    ctx = SimpleNamespace(
        settings=loaded.settings,
        runtime=loaded.runtime,
        config_dict=flat_config(loaded),
    )
    legacy_ctx = SimpleNamespace(
        config_dict={"max_steps": 3, "launch_root": "/r"}
    )
    for source in (loaded, ctx, legacy_ctx, flat_config(loaded)):
        assert settings_of(source).agent.max_steps == 3
        assert runtime_of(source).launch_root == "/r"
    assert settings_of(loaded.settings).agent.max_steps == 3


def test_settings_of_converts_a_frozen_view_once():
    view = flat_config(settings_from_flat({"max_steps": 3}))
    assert settings_of(view) is settings_of(view)


def test_legacy_flat_dict_is_validated_strictly():
    with pytest.raises(ValidationError):
        settings_of({"max_steps": "много"})
    with pytest.raises(ValidationError):
        settings_of({"no_such_setting": 1})


def test_subagent_tool_scope_lives_in_runtime():
    loaded = settings_from_flat(
        {"subagent_tools_allow": ["a"], "subagent_tools_stop": ["b"]}
    )
    assert loaded.runtime.subagent_tools_allow == ("a",)
    assert loaded.runtime.subagent_tools_stop == ("b",)
    flat = flat_config(loaded)
    assert flat["subagent_tools_allow"] == ["a"]
    assert flat["subagent_tools_stop"] == ["b"]


def test_is_explicit_follows_fields_set_along_the_path():
    settings = override_settings(
        default_settings(), {"tui.finish_gate_mode": "smart"}
    )
    assert is_explicit(settings, "tui.finish_gate_mode")
    assert not is_explicit(settings, "tui.text_answer_mode")
    assert not is_explicit(default_settings(), "tui.finish_gate_mode")


def test_override_reaches_aliased_field_inside_dict_of_models():
    # У `dict[str, Model]` модель лежит в аргументах аннотации: ключ записи
    # (имя сервера) не должен съедаться как поле, а поле — терять свой alias.
    settings = override_settings(
        default_settings(),
        {"mcp.servers": {"x": {"transport": "stdio", "command": "echo"}}},
    )
    updated = override_settings(
        settings, {"mcp.servers.x.server_name": "renamed"}
    )
    assert updated.mcp.servers["x"].server_name == "renamed"
