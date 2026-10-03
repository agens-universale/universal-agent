"""The harbor adapter must not invent its own config — it must start from the schema.

A bench runner has no YAML, so ``load_settings`` cannot run and the adapter assembles
the settings itself. While it hand-rolled a flat dict, every key added to ``AppConfig``
and read hard by a consumer was absent there, and the whole bench died with KeyError.
Now the adapter builds a real ``AppConfig`` (``LoadedSettings``) on top of the schema
defaults, so the guarantee is stated in typed terms:

* the result validates as ``AppConfig`` — every field exists by construction;
* nothing differs from the schema defaults except what the bench and the profile
  meant to change (no hidden values sneak in, no field goes missing);
* profile keys (old flat names) land on their setting paths; a typo fails loudly.

Layering under test: schema defaults < bench defaults < profile overrides < runtime.
"""

import sys

import pytest
from gc_core.config_modules.root import AppConfig, resolve_paths
from gc_core.settings import LoadedSettings
from gc_testkit.paths import HARBOR
from pydantic import ValidationError

_HARBOR_DIR = HARBOR
if str(_HARBOR_DIR) not in sys.path:
    sys.path.insert(0, str(_HARBOR_DIR))

from adapters.gc_cli_config import (
    bench_defaults,
    build_gc_cli_settings,
)
from gc_testkit.paths import ASSETS

_PROMPTS = ASSETS / "prompts"
_SKILLS = ASSETS / "skills"
_URL = "http://127.0.0.1:9999/v1/chat/completions"


def _loaded(overrides: dict | None = None) -> LoadedSettings:
    return build_gc_cli_settings(
        completions_url=_URL,
        model="test-model",
        prompts_dir=_PROMPTS,
        skills_dir=_SKILLS,
        config_dir=str(_HARBOR_DIR),
        overrides=overrides,
    )


def _config(overrides: dict | None = None) -> AppConfig:
    return _loaded(overrides).settings


def _schema_defaults() -> AppConfig:
    return resolve_paths(AppConfig.model_validate({}), str(_HARBOR_DIR))


def _leaves(value, prefix=()):
    """Плоский вид дампа модели: путь листа → значение.

    Словари раскрываются до листьев; пустой словарь листа не даёт — иначе
    наполненный `{}` считался бы изменением и самого словаря, и его ключа.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _leaves(item, (*prefix, str(key)))
    else:
        yield ".".join(prefix), value


def _changed_paths(a: AppConfig, b: AppConfig) -> set[str]:
    left = dict(_leaves(a.model_dump(mode="json")))
    right = dict(_leaves(b.model_dump(mode="json")))
    return {
        path
        for path in left.keys() | right.keys()
        if left.get(path) != right.get(path)
    }


def test_bench_settings_validate_as_app_config():
    """Every schema field exists: the settings re-validate from their own dump."""
    config = _config()

    assert isinstance(config, AppConfig)
    assert AppConfig.model_validate(config.model_dump(by_alias=True)) == config


def test_only_intended_fields_differ_from_schema_defaults():
    """Schema defaults < bench defaults < runtime — and nothing else is touched."""
    changed = _changed_paths(_schema_defaults(), _config())
    intended = set(bench_defaults(_PROMPTS, _SKILLS)) | {
        "backend.completions_url",
        "agent.model",
    }

    assert changed <= intended, sorted(changed - intended)


def test_profile_overrides_land_on_their_setting_paths():
    """Old flat names are translated, and only the named fields change."""
    base = _config()
    config = _config(
        {
            "max_steps": 20,
            "chat_completion_kwargs": {"tool_choice": "auto"},
            "summary_params": {"max_message_chars": 1234},
            "agent.no_tool_call_max_count": 7,
        }
    )

    assert _changed_paths(base, config) == {
        "agent.max_steps",
        "backend.chat_completion_kwargs.tool_choice",
        "summary.max_message_chars",
        "agent.no_tool_call_max_count",
    }


def test_unknown_profile_key_fails_loudly():
    """A misspelt key used to ride along unread; now the schema rejects it."""
    with pytest.raises(ValidationError):
        _config({"max_stepz": 5})


def test_keys_consumers_read_hard_are_present():
    """A missing field on any of these kills every trial of a bench run."""
    config = _config()

    assert (
        config.backend.request_timeout_sec
        == AppConfig.model_validate({}).backend.request_timeout_sec
    )


def test_bench_defaults_win_over_schema_defaults():
    loaded = _loaded()
    config = loaded.settings

    assert config.backend.api_mode == "chat_completions"
    assert loaded.runtime.os_backend == "harbor"
    assert config.security.read_guard.enabled is False
    assert config.security.command_moderation.mode == "allow"
    assert config.agent.exec_timeout_sec == 600
    assert AppConfig.model_validate({}).agent.exec_timeout_sec == 120


def test_profile_can_override_runtime_os_backend():
    """`os_backend` is a runtime fact, not a setting: it goes to RuntimeEnv."""
    assert _loaded({"os_backend": "local"}).runtime.os_backend == "local"


def test_bench_does_not_set_generation_kwargs_without_profile():
    assert _config().backend.chat_completion_kwargs == {}


def test_bench_does_not_override_generation_token_settings():
    defaults = bench_defaults(_PROMPTS, _SKILLS)
    assert "backend.chat_completions_max_tokens" not in defaults
    assert "backend.use_max_tokens_param" not in defaults

    schema = _schema_defaults()
    config = _config()
    assert (
        config.backend.chat_completions_max_tokens
        == schema.backend.chat_completions_max_tokens
    )
    assert (
        config.backend.use_max_tokens_param
        == schema.backend.use_max_tokens_param
    )

    explicit = _config(
        {"chat_completions_max_tokens": 4096, "use_max_tokens_param": True}
    )
    assert explicit.backend.chat_completions_max_tokens == 4096
    assert explicit.backend.use_max_tokens_param is True


def test_bench_does_not_override_agent_run_policy():
    defaults = bench_defaults(_PROMPTS, _SKILLS)
    schema = _schema_defaults()
    for field in (
        "max_steps",
        "no_tool_call_max_count",
        "use_local_subagents",
    ):
        assert f"agent.{field}" not in defaults
        assert getattr(_config().agent, field) == getattr(schema.agent, field)

    config = _config(
        {
            "max_steps": 20,
            "no_tool_call_max_count": 7,
            "use_local_subagents": True,
        }
    )
    assert config.agent.max_steps == 20
    assert config.agent.no_tool_call_max_count == 7
    assert config.agent.use_local_subagents is True


def test_profile_overrides_win_over_bench_defaults():
    config = _config(
        {
            "max_steps": 1000,
            "stop_tools": ["turn_ask_human"],
            "exec_timeout_sec": 30,
        }
    )

    assert config.agent.max_steps == 1000
    assert config.agent.stop_tools == ["turn_ask_human"]
    assert config.agent.exec_timeout_sec == 30


def test_runtime_url_and_model_win_over_profile():
    config = _config(
        {
            "completions_url": "http://stale/v1/chat/completions",
            "model": "stale-model",
        }
    )

    assert config.backend.completions_url == _URL
    assert config.agent.model == "test-model"


def test_nested_override_merges_instead_of_replacing():
    config = _config(
        {
            "chat_completion_kwargs": {
                "tool_choice": "required",
                "chat_template_kwargs": {"enable_thinking": True},
            }
        }
    )

    assert config.backend.chat_completion_kwargs == {
        "tool_choice": "required",
        "chat_template_kwargs": {"enable_thinking": True},
    }


def test_provenance_names_the_layer_of_each_override():
    loaded = _loaded({"max_steps": 20})

    assert loaded.provenance.source("agent.max_steps").layer == "bench_profile"
    assert loaded.provenance.source("agent.exec_timeout_sec").layer == "bench"
    assert loaded.provenance.source("agent.model").layer == "runtime"


def test_profile_dict_is_not_mutated():
    overrides = {"summary_params": {"max_message_chars": 1234}}
    _config(overrides)

    assert overrides == {"summary_params": {"max_message_chars": 1234}}
