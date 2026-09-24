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

from adapters.gc_cli_config import build_gc_cli_config
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
    resolve_paths(app_cfg, str(_HARBOR_DIR))
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
    config = _config({"chat_completion_kwargs": {"tool_choice": "none"}})

    assert config["chat_completion_kwargs"]["tool_choice"] == "none"
    assert config["chat_completion_kwargs"]["chat_template_kwargs"] == {
        "enable_thinking": False
    }
