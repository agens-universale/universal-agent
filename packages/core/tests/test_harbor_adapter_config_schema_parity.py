"""The harbor adapter must not invent its own config — it must start from the schema.

A bench runner has no YAML, so ``load_settings`` cannot run and the adapter assembles
the settings itself. While it hand-rolled a flat dict, every key added to ``AppConfig``
and read hard by a consumer was absent there, and the whole bench died with KeyError.
Now the adapter builds a real ``AppConfig`` (``LoadedSettings``) on top of the schema
defaults, so the guarantee is stated in typed terms:

* the result validates as ``AppConfig`` — every field exists by construction;
* nothing differs from the schema defaults except what the bench and the profile
  meant to change (no hidden values sneak in, no field goes missing);
* profile keys (setting paths, nested sections) land on their paths; a typo or an
  old flat name fails loudly with a path hint.

Layering under test: schema defaults < bench defaults < profile overrides < runtime.
"""

import sys

import pytest
import yaml
from gc_core.config_modules.path_hints import UnknownSettingError
from gc_core.config_modules.root import AppConfig, resolve_paths
from gc_core.settings import LoadedSettings
from gc_testkit.paths import HARBOR

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
        logs_dir=_HARBOR_DIR / "logs",
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
        "paths.logs_dump_dir",
    }

    assert changed <= intended, sorted(changed - intended)


def test_profile_overrides_land_on_their_setting_paths():
    """Paths and nested sections land on their paths; only the named fields change."""
    base = _config()
    config = _config(
        {
            "agent.max_steps": 20,
            "backend.chat_completion_kwargs": {"tool_choice": "auto"},
            "summary": {"max_message_chars": 1234},
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
    with pytest.raises(UnknownSettingError):
        _config({"max_stepz": 5})
    with pytest.raises(UnknownSettingError):
        _config({"summary": {"max_message_charz": 5}})


def test_old_flat_profile_key_fails_with_path_hint():
    """Old flat ``config_dict`` names are gone; the error names the path."""
    with pytest.raises(UnknownSettingError) as exc:
        _config({"max_steps": 5})
    assert "agent.max_steps" in str(exc.value)


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
    assert config.os_backend.kind == "harbor"
    assert config.security.read_guard.enabled is False
    assert config.security.command_moderation.mode == "allow"
    assert config.agent.exec_timeout_sec == 600
    assert AppConfig.model_validate({}).agent.exec_timeout_sec == 200


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
        {
            "backend.chat_completions_max_tokens": 4096,
            "backend.use_max_tokens_param": True,
        }
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
            "agent.max_steps": 20,
            "agent.no_tool_call_max_count": 7,
            "agent.use_local_subagents": True,
        }
    )
    assert config.agent.max_steps == 20
    assert config.agent.no_tool_call_max_count == 7
    assert config.agent.use_local_subagents is True


def test_profile_overrides_win_over_bench_defaults():
    config = _config(
        {
            "agent.max_steps": 1000,
            "agent.stop_tools": ["turn_ask_human"],
            "agent.exec_timeout_sec": 30,
        }
    )

    assert config.agent.max_steps == 1000
    assert config.agent.stop_tools == ["turn_ask_human"]
    assert config.agent.exec_timeout_sec == 30


def _bench_audience(config):
    from gc_core.model_router import ModelRouter
    from gc_core.tools.registry import get_builtin_tools_data
    from gc_core.vision.audience import compute_audience, view_tool_in_set

    tools = [
        entry["spec"] for entry in get_builtin_tools_data(config).values()
    ]
    from gc_core.settings import settings_of

    router = ModelRouter(config, level_provider=lambda: "default")
    audience = compute_audience(
        router,
        request_resolved=router.resolve("main"),
        api_mode=settings_of(config).backend.api_mode,
        tool_in_set=view_tool_in_set(tools),
        settings=config,
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
    audience, _tools = _bench_audience(_config({"backend.vision": True}))

    assert audience.native is None  # true permits a probe, not pixels
    assert audience.tool_visible is True


def test_runtime_url_and_model_win_over_profile():
    config = _config(
        {
            "backend.completions_url": "http://stale/v1/chat/completions",
            "agent.model": "stale-model",
        }
    )

    assert config.backend.completions_url == _URL
    assert config.agent.model == "test-model"


def test_nested_override_merges_instead_of_replacing():
    config = _config(
        {
            "backend.chat_completion_kwargs": {
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
    loaded = _loaded({"agent.max_steps": 20})

    assert loaded.provenance.source("agent.max_steps").layer == "bench_profile"
    assert loaded.provenance.source("agent.exec_timeout_sec").layer == "bench"
    assert loaded.provenance.source("agent.model").layer == "runtime"


def test_profile_dict_is_not_mutated():
    overrides = {"summary": {"max_message_chars": 1234}}
    _config(overrides)

    assert overrides == {"summary": {"max_message_chars": 1234}}


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
        await pr._arequest_backend_canonical(
            [message],
            state,
            ctx,
            resolved=pr.resolve_main_for_call(state, ctx),
        )
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
    import base64
    import json

    def pngs(request):
        # Как `vision/test_vision_probe_flow.pngs`: импорт из соседнего каталога
        # работал только при прогоне всего набора (каталог уже в sys.path).
        return [
            base64.b64decode(part["image_url"]["url"].split(",")[1])
            for message in json.loads(request.content).get("messages", [])
            for part in message.get("content", [])
            if isinstance(part, dict) and part.get("type") == "image_url"
        ]

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
    config = _loaded({"backend.vision": True})
    ctx = _ctx(tmp_path, config, [])
    att, message = _user_with_image(ctx.image_store)
    state = _state(
        [message],
        tools=[
            entry["spec"] for entry in get_builtin_tools_data(config).values()
        ],
    )
    state.vision_probe_turn = "custom-image-bench"
    await pr._arequest_backend_canonical(
        [message], state, ctx, resolved=pr.resolve_main_for_call(state, ctx)
    )
    assert len(spy.requests) == 2
    assert pngs(spy.requests[0]) == [challenge.png]
    assert pngs(spy.requests[1]) == [ctx.image_store.read_for_send(att)]
    assert state.vision_audience.native is not None
    assert b'"fs_view_image"' in spy.requests[1].content


def test_two_level_numbers_reach_the_bench_config_and_a_profile_overrides_one_of_them():
    """Конфиг бенча растёт из типизированной схемы: подключ `two_level` обязан в
    нём быть, а профиль, назвавший одно число, второе не теряет."""
    from gc_core.auto_routing.policy import RoutingParams

    assert _config().agent.auto_routing_two_level.model_dump() == {
        "weak_steps": 10,
        "strong_steps": 2,
        "pair_threshold": 3,
    }
    config = _config(
        {
            "agent.auto_routing_two_level": {"strong_steps": 4},
        }
    )
    assert config.agent.auto_routing_two_level.model_dump() == {
        "weak_steps": 10,
        "strong_steps": 4,
        "pair_threshold": 3,
    }
    params = RoutingParams.from_config(config)
    assert (params.policy, params.weak_steps, params.strong_steps) == (
        "two_level",
        10,
        4,
    )


def test_runtime_logs_directory_wins_over_profile():
    config = _config({"paths.logs_dump_dir": "/tmp/unrelated-profile-logs"})
    assert config.paths.logs_dump_dir == str(_HARBOR_DIR / "logs")


def test_routing_defaults_reach_bench_settings_and_explicit_updates_win():
    defaults = _config().agent
    assert defaults.auto_routing is True
    assert defaults.auto_routing_safe_level == "auto"
    assert defaults.auto_routing_two_level.model_dump() == {
        "weak_steps": 10, "strong_steps": 2, "pair_threshold": 3,
    }
    config = _config({"agent.auto_routing_two_level": {"weak_steps": 6}})
    assert config.agent.auto_routing_two_level.model_dump() == {
        "weak_steps": 6, "strong_steps": 2, "pair_threshold": 3,
    }


def test_setting_os_backend_kind_stays_separate_from_runtime_backend():
    loaded = _loaded({"os_backend.kind": "local"})
    assert loaded.settings.os_backend.kind == "local"
    assert loaded.runtime.os_backend == "harbor"
