"""Public workflow policy is explicit, complete and free of transport secrets."""

import json
from copy import deepcopy
from dataclasses import asdict

import pytest
from gc_core.config import flatten_app_config
from gc_core.config_modules.root import AppConfig
from gc_core.model_router import ModelRouter
from gc_ua_temporalio.public_config import (
    PUBLIC_POLICY_SCHEMA,
    WORKER_ONLY_FIELDS,
    PublicConfigError,
    public_config_snapshot,
)
from pydantic import BaseModel


def json_value(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    return value


def test_every_real_flatten_field_is_explicitly_classified_and_policy_preserved():
    config = flatten_app_config(AppConfig())
    snapshot = public_config_snapshot(config)
    assert set(config) <= set(PUBLIC_POLICY_SCHEMA) | WORKER_ONLY_FIELDS | {
        "models"
    }
    for key in set(config) & set(PUBLIC_POLICY_SCHEMA):
        assert snapshot.config_dict[key] == json_value(config[key]), key
    json.dumps(asdict(snapshot), allow_nan=False)


def test_worker_only_connections_and_handles_never_enter_public_snapshot():
    config = flatten_app_config(AppConfig())
    config.update(
        {
            "completions_url": "https://user:SECRET_ENDPOINT@private.ru/SECRET_PATH?token=SECRET_QUERY",
            "chat_completion_headers": {"Authorization": "SECRET_HEADER"},
            "agent_auth": {"token": "SECRET_AUTH"},
            "mcp-servers": {"x": {"env": {"TOKEN": "SECRET_MCP"}}},
            "harbor_exec_init": "export TOKEN=SECRET_SHELL",
            "environment": object(),
            "auth_connector": object(),
            "voice": {"token": "SECRET_VOICE"},
            "proxy": "https://SECRET_PROXY@host",
        }
    )
    snapshot = public_config_snapshot(config)
    serialized = json.dumps(asdict(snapshot))
    assert "SECRET" not in serialized
    assert "private.ru" not in serialized
    assert "worker://" not in serialized
    assert snapshot.model_transport_configured is True
    assert snapshot.provider_currency == "₽"
    assert snapshot.model_provider_currencies == {}
    assert "completions_url" not in snapshot.config_dict
    assert "mcp-servers" not in snapshot.config_dict
    assert config["agent_auth"]["token"] == "SECRET_AUTH"


def test_routing_identity_assignments_overrides_and_limits_survive_without_url():
    config = flatten_app_config(AppConfig())
    config.update(
        {
            "level": "max",
            "completions_url": "https://secret.invalid/v1",
            "models": {
                "fast": {
                    "model": "provider-fast",
                    "url": None,
                    "authorization_header": "SECRET",
                    "pricing": None,
                    "backend_overrides": {"chat_completions_max_tokens": 1234},
                },
                "large": {
                    "model": "provider-large",
                    "url": "https://private.ru/SECRET",
                    "authorization_header": "SECRET",
                    "pricing": {"input": 2, "output": 3},
                    "backend_overrides": {
                        "chat_completion_headers": {"Authorization": "SECRET"},
                        "chat_completion_kwargs": {"temperature": 0.2},
                    },
                },
            },
            "slots": {"light": "fast", "default": "fast", "max": "large"},
            "assign": {
                "command_judge": "fast",
                "subagents": {"searcher": "large"},
            },
            "use_local_subagents": ["searcher"],
        }
    )
    original = deepcopy(config)
    public = public_config_snapshot(config)
    assert config == original
    assert public.provider_currency == "$"
    assert public.model_provider_currencies == {"fast": "$", "large": "₽"}
    worker_router = ModelRouter(config, level_provider=lambda: "max")
    public_router = ModelRouter(
        public.config_dict, level_provider=lambda: "max"
    )
    for consumer in ("main", "command_judge", "subagent:searcher"):
        worker, workflow = (
            worker_router.resolve(consumer),
            public_router.resolve(consumer),
        )
        assert (workflow.alias, workflow.model, workflow.source) == (
            worker.alias,
            worker.model,
            worker.source,
        )
        assert not workflow.url and not workflow.authorization
    assert (
        public.config_dict["models"]["fast"]["backend_overrides"][
            "chat_completions_max_tokens"
        ]
        == 1234
    )
    assert public.config_dict["use_local_subagents"] == ["searcher"]
    assert "SECRET" not in json.dumps(asdict(public))


@pytest.mark.parametrize(
    "overlay",
    [
        {"unknown_credentials": "SECRET"},
        {"read_guard": {"enabled": True, "new_auth": "SECRET"}},
        {"chat_completion_kwargs": {"api_key": "SECRET"}},
        {
            "auxiliary_llm_params": {
                "completion_kwargs": {"headers": {"Authorization": "SECRET"}}
            }
        },
        {"models": {"m": {"model": "x", "new_password": "SECRET"}}},
        {
            "models": {
                "m": {
                    "model": "x",
                    "backend_overrides": {"new_password": "SECRET"},
                }
            }
        },
        {"os_backend": object()},
    ],
)
def test_unknown_and_unsafe_extensions_fail_without_value_in_exception(
    overlay,
):
    with pytest.raises(PublicConfigError) as error:
        public_config_snapshot(overlay)
    assert "SECRET" not in str(error.value)


def test_core_policy_nested_fields_and_aux_provider_parameters_are_preserved():
    config = {
        "intent_scope_params": {
            "tools": {"mcp_x": {"record": "raw", "store_arguments": False}}
        },
        "inject_tool_fields": {"reason": "Explain why"},
        "tool_usage_notice_params": [
            {
                "tool_name": "session_memory",
                "step_messages_count": 8,
                "notice_message": "Remember",
            }
        ],
        "auxiliary_llm_params": {
            "judge_max_tokens": 123,
            "completion_kwargs": {
                "thinking": {"type": "enabled", "budget_tokens": 32}
            },
        },
        "protected_paths": {"enabled": False},
        "os_backend": "harbor",
        "launch_root": "/workspace",
        "project_root": "/workspace",
        "level": "light",
    }
    snapshot = public_config_snapshot(config)
    assert snapshot.config_dict == config
    snapshot.config_dict["intent_scope_params"]["tools"]["mcp_x"]["record"] = (
        "none"
    )
    assert config["intent_scope_params"]["tools"]["mcp_x"]["record"] == "raw"
