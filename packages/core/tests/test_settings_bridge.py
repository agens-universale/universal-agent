"""Мост к настройкам: потребители читают ``settings_of``/``runtime_of``.

Мост принимает только контекст рана, ``LoadedSettings``, ``AppConfig`` или
``None``; плоский словарь — ``TypeError``. Тестовый ``make_settings``
(пути через точку и секции) обязан давать те же настройки, что и
типизированный путь, — без потерь, включая признаки «задано явно».
"""

from types import SimpleNamespace

import pytest
from gc_core.settings import (
    RuntimeEnv,
    default_settings,
    is_explicit,
    override_settings,
    runtime_of,
    settings_of,
)
from gc_testkit.settings import make_settings
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
            "tui.finish_gate_mode": "always",
        },
    )


@pytest.mark.parametrize("settings", [default_settings(), _rich_settings()])
def test_make_settings_round_trip_is_lossless(settings):
    # Явно заданное (dump с exclude_unset) → тестовый мост → те же настройки
    # и та же явность полей; рантайм-ключи уходят в RuntimeEnv.
    explicit = settings.model_dump(by_alias=True, exclude_unset=True)
    back = make_settings({**explicit, "launch_root": "/w"})
    assert back.settings == settings
    assert back.settings.model_dump(exclude_unset=True) == settings.model_dump(
        exclude_unset=True
    )
    assert back.runtime.launch_root == "/w"


def test_settings_of_reads_every_source_the_same_way():
    loaded = make_settings({"agent.max_steps": 3, "launch_root": "/r"})
    ctx = SimpleNamespace(settings=loaded.settings, runtime=loaded.runtime)
    for source in (loaded, ctx):
        assert settings_of(source).agent.max_steps == 3
        assert runtime_of(source).launch_root == "/r"
    assert settings_of(loaded.settings).agent.max_steps == 3


def test_settings_of_rejects_flat_dicts():
    # Плоский словарь больше не источник настроек — ни сам, ни в контексте.
    with pytest.raises(TypeError):
        settings_of({"agent.max_steps": 3})
    with pytest.raises(TypeError):
        runtime_of({"launch_root": "/r"})
    with pytest.raises(TypeError):
        settings_of(SimpleNamespace(config_dict={"agent.max_steps": 3}))


def test_test_settings_are_validated_strictly():
    with pytest.raises(ValidationError):
        make_settings({"agent.max_steps": "много"})
    with pytest.raises(ValidationError):
        make_settings({"no_such_setting": 1})
    # Прежних плоских имён нет: `max_steps` — неизвестный ключ.
    with pytest.raises(ValidationError):
        make_settings({"max_steps": 3})


def test_test_settings_accept_paths_and_sections():
    by_path = make_settings({"agent.max_steps": 3}).settings
    by_section = make_settings({"agent": {"max_steps": 3}}).settings
    assert by_path.agent.max_steps == by_section.agent.max_steps == 3


def test_subagent_tool_scope_lives_in_runtime():
    loaded = make_settings(
        {"subagent_tools_allow": ["a"], "subagent_tools_stop": ["b"]}
    )
    assert loaded.runtime.subagent_tools_allow == ("a",)
    assert loaded.runtime.subagent_tools_stop == ("b",)


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


def test_override_replaces_lists_and_dicts_instead_of_merging(tmp_path):
    """CLI/сервер/сессия сужают: правка заменяет значение по своему пути."""
    base = override_settings(
        default_settings(),
        {
            "security.read_guard.allowed_read_roots": [str(tmp_path / "file")],
            "mcp.servers": {"github": {"transport": "stdio", "command": "x"}},
            "models": {
                "m1": {
                    "model": "prov/m1",
                    "url": "https://file.example/v1/chat/completions",
                    "token": "file-token",
                }
            },
            "agent.stop_tools": ["turn_finish", "ask_human"],
        },
    )
    narrowed = override_settings(
        base,
        {
            "security.read_guard.allowed_read_roots": [str(tmp_path / "cli")],
            "mcp.servers": {},
            "models.m1": {"model": "prov/other"},
            "agent.stop_tools": ["turn_finish"],
        },
    )
    assert narrowed.security.read_guard.allowed_read_roots == [
        str(tmp_path / "cli")
    ]
    assert narrowed.mcp.servers == {}
    model = narrowed.models["m1"]
    assert (model.model, model.url, model.token) == ("prov/other", None, None)
    assert narrowed.agent.stop_tools == ["turn_finish"]
    # Соседние явные значения не теряются.
    assert (
        base.security.read_guard.enabled
        == narrowed.security.read_guard.enabled
    )


def test_expand_section_updates_keeps_dict_leaves_and_model_entries():
    from gc_core.settings import expand_section_updates

    assert expand_section_updates(
        {
            "summary": {"max_message_chars": 10},
            "backend.chat_completion_kwargs": {"temperature": 0},
            "models": {"m": {"model": "x"}},
        }
    ) == {
        "summary.max_message_chars": 10,
        "backend.chat_completion_kwargs": {"temperature": 0},
        "models": {"m": {"model": "x"}},
    }


def test_section_override_keeps_other_fields_and_pair_rule_holds(tmp_path):
    """Секция раскладывается на листья (allowed_read_roots не сбрасывается),
    а половина пары адрес/токен записи реестра — ошибка, как в файле."""
    base = override_settings(
        default_settings(),
        {
            "security.read_guard.allowed_read_roots": [str(tmp_path)],
            "models": {
                "ds": {
                    "model": "x",
                    "url": "https://a.example/v1/chat/completions",
                    "token": "t",
                }
            },
        },
    )
    changed = override_settings(base, {"security.read_guard": {"mode": "ask"}})
    assert changed.security.read_guard.mode == "ask"
    assert changed.security.read_guard.allowed_read_roots == [str(tmp_path)]
    with pytest.raises(ValueError, match="models.ds"):
        override_settings(
            base, {"models.ds.url": "https://evil.example/v1/chat/completions"}
        )


def test_section_override_marks_only_its_leaves_in_provenance():
    """`--agent '{"max_steps": 2}'` не делает agent.model «из CLI»
    (иначе включался бы легаси-форс модели)."""
    from gc_core.config import cli_forces_legacy_model
    from gc_core.settings import LoadedSettings

    loaded = LoadedSettings(
        settings=override_settings(
            default_settings(), {"models": {"ds": {"model": "x"}}}
        )
    ).with_overrides({"agent": {"max_steps": 2}}, layer="cli")
    assert loaded.settings.agent.max_steps == 2
    assert loaded.provenance.source("agent.max_steps").layer == "cli"
    assert loaded.provenance.source("agent.model").layer != "cli"
    assert cli_forces_legacy_model(loaded) is False


def test_channel_declarations_are_runtime_fields():
    """Канал объявляет о себе рантаймом: по умолчанию «не объявлено» (ядро
    проверяет человека само) и слова пользователя свои."""
    runtime = RuntimeEnv()
    assert runtime.human_channel is None
    assert runtime.external_user_words is False
    declared = (
        make_settings()
        .with_runtime(human_channel=False, external_user_words=True)
        .runtime
    )
    assert declared.human_channel is False
    assert declared.external_user_words is True
