"""The harbor adapter must not invent its own config — it must start from the schema.

A bench runner has no YAML, so ``load_config`` cannot run and the adapter assembles
``config_dict`` itself. While it hand-rolled that dict, every key added to ``AppConfig``
and read hard by a consumer (``cfg["backend_request_timeout_sec"]`` in processing_responses /
llm_chat_client / subagents.special_agent) was absent there, and the whole bench died with
KeyError. Building on top of ``flatten_app_config(AppConfig())`` makes that impossible.

Layering under test: schema defaults < bench defaults < profile overrides < runtime.
"""

import sys

from gc_core.config import flatten_app_config
from gc_core.config_modules.root import AppConfig, resolve_paths
from gc_testkit.paths import HARBOR

_HARBOR_DIR = HARBOR
if str(_HARBOR_DIR) not in sys.path:
    sys.path.insert(0, str(_HARBOR_DIR))

from adapters.gc_cli_config import (
    bench_defaults,
    build_gc_cli_config,
)
from gc_testkit.paths import ASSETS

_PROMPTS = ASSETS / "prompts"
_SKILLS = ASSETS / "skills"


def _config(overrides: dict | None = None) -> dict:
    return build_gc_cli_config(
        completions_url="http://127.0.0.1:9999/v1/chat/completions",
        model="test-model",
        prompts_dir=_PROMPTS,
        skills_dir=_SKILLS,
        config_dir=str(_HARBOR_DIR),
        overrides=overrides,
    )


def _schema_defaults() -> dict:
    app_cfg = AppConfig.model_validate({})
    app_cfg = resolve_paths(app_cfg, str(_HARBOR_DIR))
    return flatten_app_config(app_cfg)


def test_bench_config_covers_every_schema_key():
    missing = sorted(set(_schema_defaults()) - set(_config()))

    assert missing == [], (
        f"schema keys absent from the bench config: {missing}"
    )


def test_keys_consumers_read_hard_are_present():
    """A KeyError on any of these kills every trial of a bench run."""
    config = _config()

    assert (
        config["backend_request_timeout_sec"]
        == AppConfig.model_validate({}).backend.request_timeout_sec
    )


def test_bench_defaults_win_over_schema_defaults():
    config = _config()

    assert config["backend_api_mode"] == "chat_completions"
    assert config["os_backend"] == "harbor"
    assert config["read_guard"]["enabled"] is False
    assert config["command_moderation"]["mode"] == "allow"
    assert config["exec_timeout_sec"] == 600
    assert AppConfig.model_validate({}).agent.exec_timeout_sec == 120


def test_bench_does_not_set_generation_kwargs_without_profile():
    assert _config()["chat_completion_kwargs"] == {}


def test_bench_does_not_override_generation_token_settings():
    defaults = bench_defaults(_PROMPTS, _SKILLS)
    assert "chat_completions_max_tokens" not in defaults
    assert "use_max_tokens_param" not in defaults

    schema = _schema_defaults()
    config = _config()
    assert (
        config["chat_completions_max_tokens"]
        == schema["chat_completions_max_tokens"]
    )
    assert config["use_max_tokens_param"] == schema["use_max_tokens_param"]

    explicit = _config(
        {"chat_completions_max_tokens": 4096, "use_max_tokens_param": True}
    )
    assert explicit["chat_completions_max_tokens"] == 4096
    assert explicit["use_max_tokens_param"] is True


def test_bench_does_not_override_agent_run_policy():
    defaults = bench_defaults(_PROMPTS, _SKILLS)
    schema = _schema_defaults()
    for key in ("max_steps", "no_tool_call_max_count", "use_local_subagents"):
        assert key not in defaults
        assert _config()[key] == schema[key]

    config = _config(
        {
            "max_steps": 20,
            "no_tool_call_max_count": 7,
            "use_local_subagents": True,
        }
    )
    assert config["max_steps"] == 20
    assert config["no_tool_call_max_count"] == 7
    assert config["use_local_subagents"] is True


def test_profile_overrides_win_over_bench_defaults():
    config = _config({"max_steps": 1000, "stop_tools": ["turn_ask_human"]})

    assert config["max_steps"] == 1000
    assert config["stop_tools"] == ["turn_ask_human"]


def test_runtime_url_and_model_win_over_profile():
    config = _config(
        {
            "completions_url": "http://stale/v1/chat/completions",
            "model": "stale-model",
        }
    )

    assert (
        config["completions_url"]
        == "http://127.0.0.1:9999/v1/chat/completions"
    )
    assert config["model"] == "test-model"


def test_nested_override_merges_instead_of_replacing():
    config = _config(
        {
            "chat_completion_kwargs": {
                "tool_choice": "required",
                "chat_template_kwargs": {"enable_thinking": True},
            }
        }
    )

    assert config["chat_completion_kwargs"] == {
        "tool_choice": "required",
        "chat_template_kwargs": {"enable_thinking": True},
    }
