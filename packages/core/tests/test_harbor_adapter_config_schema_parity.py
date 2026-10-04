"""The harbor adapter must not invent its own config — it must start from the schema.

A bench runner has no YAML, so ``load_config`` cannot run and the adapter assembles
``config_dict`` itself. While it hand-rolled that dict, every key added to ``AppConfig``
and read hard by a consumer (``cfg["backend_request_timeout_sec"]`` in processing_responses /
llm_chat_client / subagents.special_agent) was absent there, and the whole bench died with
KeyError. Building on top of ``flatten_app_config(AppConfig())`` makes that impossible.

Layering under test: schema defaults < bench defaults < profile overrides < runtime.
"""

import sys

import pytest
import yaml
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


def _bench_audience(config):
    from gc_core.model_router import ModelRouter
    from gc_core.tools.registry import get_builtin_tools_data
    from gc_core.vision.audience import compute_audience, view_tool_in_set

    tools = [
        entry["spec"] for entry in get_builtin_tools_data(config).values()
    ]
    router = ModelRouter(config, level_provider=lambda: "default")
    audience = compute_audience(
        router,
        request_resolved=router.resolve("main"),
        api_mode=config["backend_api_mode"],
        tool_in_set=view_tool_in_set(tools),
        config=config,
    )
    return audience, tools


def test_text_bench_does_not_send_pixels_or_offer_image_viewing():
    from gc_core.vision.audience import tools_for_request

    audience, tools = _bench_audience(_config())

    assert audience.nobody is True
    assert "fs_view_image" not in {
        tool["name"] for tool in tools_for_request(tools, audience)
    }


@pytest.mark.parametrize(
    "profile_name",
    [
        "bench_01_no_summary_no_subagents.yaml",
        "bench_02_summary_no_subagents.yaml",
        "bench_03_no_summary_subagents.yaml",
        "bench_04_summary_subagents.yaml",
    ],
)
def test_stock_text_profiles_exclude_image_viewing(profile_name):
    from gc_core.tools.subagents.runner import SubagentRunner

    profile = yaml.safe_load(
        (_HARBOR_DIR / "profiles" / profile_name).read_text()
    )
    config = _config(profile["agent_kwargs"]["gc_cli_config"])
    audience, tools = _bench_audience(config)

    assert audience.nobody is True
    assert "fs_view_image" not in {tool["name"] for tool in tools}

    runner = SubagentRunner(None, None, config)
    runner._active_tool_name = "subagent_coder"
    child_audience, child_tools = _bench_audience(runner._build_child_config())
    assert child_audience.nobody is True
    assert "fs_view_image" not in {tool["name"] for tool in child_tools}


def test_a_vision_experiment_can_explicitly_enable_images():
    audience, _tools = _bench_audience(_config({"backend_vision": True}))

    assert audience.native is None  # true permits a probe, not pixels
    assert audience.tool_visible is True


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


@pytest.mark.parametrize(
    "profile_name",
    [
        "bench_01_no_summary_no_subagents.yaml",
        "bench_02_summary_no_subagents.yaml",
        "bench_03_no_summary_subagents.yaml",
        "bench_04_summary_subagents.yaml",
    ],
)
async def test_stock_profiles_and_children_make_no_probe_with_attachment(
    tmp_path, monkeypatch, profile_name
):
    import httpx
    from gc_core import processing_responses as pr
    from gc_core.tools.registry import get_builtin_tools_data
    from gc_core.tools.subagents.runner import SubagentRunner
    from test_image_rejection_retry import OK, _ctx, _state, _user_with_image
    from transport_fakes import install_transport

    profile = yaml.safe_load(
        (_HARBOR_DIR / "profiles" / profile_name).read_text()
    )
    config = _config(profile["agent_kwargs"]["gc_cli_config"])
    runner = SubagentRunner(None, None, config)
    runner._active_tool_name = "subagent_coder"
    spy = install_transport(
        monkeypatch, lambda request: httpx.Response(200, json=OK)
    )
    for cfg in [config, runner._build_child_config()]:
        ctx = _ctx(tmp_path, cfg, [])
        _, message = _user_with_image(ctx.image_store)
        state = _state(
            [message],
            tools=[
                entry["spec"] for entry in get_builtin_tools_data(cfg).values()
            ],
        )
        state.vision_probe_turn = "bench-root"
        await pr._arequest_backend_canonical([message], state, ctx)
    assert len(spy.requests) == 2  # exactly the two working text calls
    assert all(
        b'"image_url"' not in r.content and b'"fs_view_image"' not in r.content
        for r in spy.requests
    )


async def test_custom_image_bench_requires_successful_probe(
    tmp_path, monkeypatch
):
    import httpx
    from gc_core import processing_responses as pr
    from gc_core.tools.registry import get_builtin_tools_data
    from gc_core.vision import probe
    from test_image_rejection_retry import OK, _ctx, _state, _user_with_image
    from test_vision_probe_flow import pngs
    from transport_fakes import install_transport

    challenge = probe.make_challenge(retry=False)
    monkeypatch.setattr(probe, "make_challenge", lambda **kwargs: challenge)
    spy = install_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={"choices": [{"message": {"content": challenge.code}}]}
            if pngs(request) == [challenge.png]
            else OK,
        ),
    )
    config = _config({"backend_vision": True})
    ctx = _ctx(tmp_path, config, [])
    att, message = _user_with_image(ctx.image_store)
    state = _state(
        [message],
        tools=[
            entry["spec"] for entry in get_builtin_tools_data(config).values()
        ],
    )
    state.vision_probe_turn = "custom-image-bench"
    await pr._arequest_backend_canonical([message], state, ctx)
    assert len(spy.requests) == 2
    assert pngs(spy.requests[0]) == [challenge.png]
    assert pngs(spy.requests[1]) == [ctx.image_store.read_for_send(att)]
    assert state.vision_audience.native is not None
    assert b'"fs_view_image"' in spy.requests[1].content
