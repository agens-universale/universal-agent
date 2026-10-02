"""Explicit public policy snapshot of the native flattened worker config.

No credentials, endpoint stand-ins, live handles or MCP connection definitions
enter this snapshot. New fields require explicit classification, including
nested provider kwargs; rejecting an unknown field never prints its value.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from gc_core.config_modules.field_meta import meta_for
from gc_core.config_modules.flat_keys import FLAT_TO_PATH
from gc_core.usage.currency import provider_currency_for_url
from pydantic import BaseModel


class PublicConfigError(ValueError):
    pass


@dataclass(frozen=True)
class PublicConfigSnapshot:
    config_dict: dict[str, Any]
    model_transport_configured: bool
    provider_currency: str
    model_provider_currencies: dict[str, str]


# Не настройки схемы: рантайм-объекты, кэши и служебные признаки плоского
# config_dict, которые воркеру/фронтенду нужны, а в снимок попасть не должны.
_WORKER_ONLY_RUNTIME_FIELDS = frozenset(
    [
        "_subagent_definitions_cache",  # кэш определений сабагентов
        "_subagent_diagnostics",  # диагностика загрузки сабагентов
        "auth_connector",  # живой AuthConnector (agent_auth)
        "backend_connection",  # живое подключение к backend
        "compat_notices",  # уведомления совместимости загрузчика
        "env",  # окружение процесса
        "environment",  # окружение исполнения
        "headers",  # собранные заголовки запроса
        "os_backend_client",  # живой клиент os_backend
        "tools_cache",  # кэш MCP discovery
        "voice_overridden_by_managed",  # признак перекрытия voice managed-слоем
    ]
)


def _schema_worker_only_fields() -> frozenset[str]:
    """Плоские имена настроек, помеченных в схеме ``gc_meta(worker_only=True)``
    (флаг секции — у всех её листьев)."""
    return frozenset(
        flat
        for flat, path in FLAT_TO_PATH.items()
        if meta_for(path).worker_only
    )


# Explicitly worker/frontend-only. Contents are never traversed or serialized.
# Настройки — по флагу worker_only в схеме; прочее — явный список выше.
WORKER_ONLY_FIELDS = _schema_worker_only_fields() | _WORKER_ONLY_RUNTIME_FIELDS


def _fields(names: str) -> dict[str, str]:
    return dict.fromkeys(names.split(), "scalar")


_COMPLETION_PARAMETERS = {
    **_fields(
        "stream temperature top_p top_k min_p frequency_penalty presence_penalty repetition_penalty max_tokens max_completion_tokens reasoning_effort seed logprobs top_logprobs n parallel_tool_calls store verbosity enable_thinking thinking_budget max_output_tokens"
    ),
    "stop": "scalar_or_list",
    "reasoning": _fields("effort max_tokens exclude enabled summary"),
    "thinking": _fields("type budget_tokens"),
    "chat_template_kwargs": _fields("enable_thinking thinking_budget"),
    "response_format": {
        **_fields("type"),
        "json_schema": {
            **_fields("name description strict"),
            "schema": "json",
        },
    },
}

_AUXILIARY_PARAMETERS = {
    **_fields("max_tokens judge_max_tokens subagent_role_max_tokens"),
    "completion_kwargs": _COMPLETION_PARAMETERS,
}

_MEMORY_LIMITS = _fields(
    "max_chars_goal max_chars_constraints max_chars_state max_chars_knowledge total_budget_chars"
)

_PROJECT_MEMORY = _fields(
    "enable_project_memory_notice check_interval_messages max_chars max_append_chars compaction_max_tokens"
)

_INTENT_SCOPE = {
    **_fields(
        "max_summary_chars max_tool_output_chars max_raw_record_chars max_final_answer_raw_chars read_page_record_limit fallback_raw_on_llm_error intent_scope_tool_summary_prompt_path intent_scope_final_answer_summary_prompt_path"
    ),
    "tools": {"*": _fields("record store_arguments store_output")},
}

_NOTICE_RULE = _fields("tool_name step_messages_count notice_message")

_TARIFF = _fields("input output cache_read currency per_tokens")

# Static policy shapes; arbitrary names exist only in typed policy maps.
_GUARD_BASE_SCHEMA = _fields("enabled mode")


PUBLIC_POLICY_SCHEMA = {
    "subagent_tools_allow": "scalar_or_list",
    "subagent_tools_stop": ["scalar"],
    **_fields(
        "backend_api_mode chat_completions_max_tokens chat_completions_min_response_tokens use_max_tokens_param openai_strict_tools_schema send_tool_strict claude_prompt_cache use_retry retry_embedded_choice_errors backend_request_timeout_sec stream tui_stream tui_stream_display tui_text_answer_mode tui_finish_gate_mode tui_finish_gate_mode_explicit strict_turn_finish_explicit_false working_memory_max_chars_goal working_memory_max_chars_constraints working_memory_max_chars_state working_memory_max_chars_knowledge working_memory_total_budget_chars project_memory_check_interval_messages project_memory_max_chars project_memory_max_append_chars enable_project_memory_notice agent_md_path chat_completions_system_prompt_path chat_completions_intent_guard_prompt_path chat_completions_custom_subagent_system_prompt_path chat_completions_special_instructions_path threads_data_path thread_store_persist skills_data_path logs_dump_dir agentsmd_override model system_prompt_id use_server_subagents subagent_max_steps subagents_dir subagents_max_count verbose server_tool_prefix max_steps session_token_limit max_tool_calls no_tool_call_max_count max_unknown_tool_repeats strict_turn_finish_validation finish_gate_max_iterations subagent_finish_gate_max_iterations finish_gate_use_rule_hints console_trace save_logs logging_format log_level limit_project_metadata tool_output_inline_max_chars stored_tool_outputs_max_entries exec_timeout_sec exec_output_capture_max_chars exec_output_preview_edge_chars walk_output_max_chars model_steering limit_trace_summary profile launch_root project_root level trace_id os_backend full_log_backend voice_overridden_by_managed"
    ),
    "chat_completion_kwargs": _COMPLETION_PARAMETERS,
    "deprecated_keys_used": ["scalar"],
    "command_moderation": {
        **_GUARD_BASE_SCHEMA,
        "allowed_command_examples": ["scalar"],
        "denied_command_examples": ["scalar"],
    },
    "write_guard": {**_GUARD_BASE_SCHEMA, "extra_paths": ["scalar"]},
    "read_guard": {
        **_fields("enabled mode max_file_size_kb binary_read_policy"),
        "allowed_read_roots": ["scalar"],
        "allowed_read_paths": ["scalar"],
        "denied_read_paths": ["scalar"],
        "denied_file_patterns": ["scalar"],
        "denied_extensions": ["scalar"],
        "binary_allowed_types": ["scalar"],
        "binary_allowed_extensions": ["scalar"],
    },
    "url_policy": {
        **_GUARD_BASE_SCHEMA,
        "allowed_schemes": ["scalar"],
        "url_blacklist": ["scalar"],
    },
    "download": _fields("download_dir max_bytes"),
    "summary_params": _fields(
        "chat_completions_summary_prompt_path chat_completions_memory_patch_replace_prompt_path chat_completions_summary_trigger_chars chat_completions_summary_turn_end_trigger_chars chat_completions_summary_trigger_tokens chat_completions_summary_turn_end_trigger_tokens incremental_batch_chars max_message_chars max_dialogue_chars max_accumulated_summary_chars last_dialog_steps_count last_dialog_step_message_max_chars intent_log_preview_chars"
    ),
    "chat_completions_params": {"auxiliary_llm": _AUXILIARY_PARAMETERS},
    "auxiliary_llm_params": _AUXILIARY_PARAMETERS,
    "quality_gate_params": _fields("max_dialogue_chars max_message_chars"),
    "working_memory": _MEMORY_LIMITS,
    "working_memory_params": _MEMORY_LIMITS,
    "todo_params": _fields("reminder_steps"),
    "tool_usage_notice": [_NOTICE_RULE],
    "tool_usage_notice_params": [_NOTICE_RULE],
    "project_memory": _PROJECT_MEMORY,
    "project_memory_params": _PROJECT_MEMORY,
    "intent_scope": _INTENT_SCOPE,
    "intent_scope_params": _INTENT_SCOPE,
    "logging_params": {
        **_fields(
            "full_log_backend console_trace save_logs format logging_format log_level propagate"
        ),
        "redact_secrets": {
            **_fields("enabled"),
            "extra_keys": ["scalar"],
            "extra_patterns": ["scalar"],
        },
    },
    "use_local_subagents": "scalar_or_list",
    "stop_tools": ["scalar"],
    "compat_notices": ["scalar"],
    "disable_skills": ["scalar"],
    "redact_secrets": {
        **_fields("enabled"),
        "extra_keys": ["scalar"],
        "extra_patterns": ["scalar"],
    },
    "inject_tool_fields": _fields("*"),
    "loop_detector": _fields("enabled"),
    "gc": _fields("force_collect"),
    "pricing": {
        **_fields("enabled currency per_tokens input output cache_read"),
        "models": {"*": _TARIFF},
    },
    "diff_tui": _fields(
        "cache_max_orig preview_context_lines full_file_max_lines max_diff_chars"
    ),
    "fs_tools": {
        **_fields("max_read_bytes max_mutate_bytes"),
        "find_in_files": {
            **_fields(
                "max_file_bytes max_candidates_like max_candidates_or_simple max_candidates_affix_or max_candidates_complex max_unfiltered_host_reads max_open_files_affix_or content_workers_threshold max_exclude_dirs top_level_dirs_cap default_limit max_total_matches exclude_dot_dirs"
            ),
            "exclude_dir_names": ["scalar"],
        },
        "find_files_by_name": _fields(
            "default_max_matches hard_max_matches max_visited"
        ),
    },
    "strip_tags_projection": {**_fields("strip_think"), "tags": ["scalar"]},
    "slots": _fields("light default max"),
    "assign": {
        **_fields(
            "finish_gate text_answer_judge command_judge summary intent_scope memory_compaction auto_judge"
        ),
        "subagents": _fields("*"),
    },
    "cli_explicit_keys": ["scalar"],
    "protected_paths": _fields("enabled"),
}

_BACKEND_PUBLIC_FIELDS = {
    "api_mode": "scalar",
    "chat_completions_max_tokens": "scalar",
    "chat_completions_min_response_tokens": "scalar",
    "use_max_tokens_param": "scalar",
    "openai_strict_tools_schema": "scalar",
    "send_tool_strict": "scalar",
    "claude_prompt_cache": "scalar",
    "use_retry": "scalar",
    "retry_embedded_choice_errors": "scalar",
    "request_timeout_sec": "scalar",
    "stream": "scalar",
    "chat_completion_kwargs": PUBLIC_POLICY_SCHEMA["chat_completion_kwargs"],
}
_BACKEND_WORKER_FIELDS = frozenset(
    {
        "completions_url",
        "chat_completion_headers",
        "use_ssl",
        "cert_path",
        "key_path",
        "proxy",
    }
)
_MODEL_FIELDS = frozenset(
    {"model", "url", "authorization_header", "pricing", "backend_overrides"}
)


def _json_value(value: Any, path: str) -> Any:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, list):
        return [_json_value(item, f"{path}[]") for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {
            key: _json_value(item, f"{path}.{key}")
            for key, item in value.items()
        }
    raise PublicConfigError(f"Non-JSON public value at {path}")


def _copy_policy(value: Any, schema: Any, path: str) -> Any:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if schema == "json":
        return _json_value(value, path)
    if schema == "scalar_or_list":
        return _copy_policy(
            value, ["scalar"] if isinstance(value, list) else "scalar", path
        )
    if schema == "scalar":
        if isinstance(value, (list, dict)):
            raise PublicConfigError(
                f"Expected a scalar public value at {path}"
            )
        return _json_value(value, path)
    if isinstance(schema, list):
        if not isinstance(value, list):
            raise PublicConfigError(f"Expected a public list at {path}")
        return [_copy_policy(item, schema[0], f"{path}[]") for item in value]
    return _copy_object_policy(value, schema, path)


def _copy_object_policy(value, schema, path):
    if not isinstance(value, dict):
        raise PublicConfigError(f"Expected a public object at {path}")
    output = {}
    for key, item in value.items():
        if not isinstance(key, str) or (
            key not in schema and "*" not in schema
        ):
            raise PublicConfigError(
                f"Unclassified public field at {path}.{key}"
            )
        output[key] = _copy_policy(
            item, schema.get(key, schema.get("*")), f"{path}.{key}"
        )
    return output


def _public_models(
    models: Any, currencies: dict[str, str], inherited_url: Any
) -> dict:
    if not isinstance(models, dict):
        raise PublicConfigError("Expected an object at models")
    output = {}
    for alias, entry in models.items():
        if not isinstance(alias, str) or not isinstance(entry, dict):
            raise PublicConfigError("Invalid public model identity")
        output[alias] = _public_model(alias, entry, currencies, inherited_url)
    return output


def _public_model(alias, entry, currencies, inherited_url):
    unknown = set(entry) - _MODEL_FIELDS
    if unknown:
        raise PublicConfigError(f"Unclassified model fields at models.{alias}")
    backend = entry.get("backend_overrides") or {}
    if not isinstance(backend, dict):
        raise PublicConfigError(
            f"Expected object at models.{alias}.backend_overrides"
        )
    unknown = (
        set(backend) - set(_BACKEND_PUBLIC_FIELDS) - _BACKEND_WORKER_FIELDS
    )
    if unknown:
        raise PublicConfigError(
            f"Unclassified backend fields at models.{alias}.backend_overrides"
        )
    public_entry = {}
    if "model" in entry:
        public_entry["model"] = _copy_policy(
            entry["model"], "scalar", f"models.{alias}.model"
        )
    if "pricing" in entry:
        public_entry["pricing"] = (
            None
            if entry["pricing"] is None
            else _copy_policy(
                entry["pricing"],
                PUBLIC_POLICY_SCHEMA["pricing"]["models"]["*"],
                f"models.{alias}.pricing",
            )
        )
    public_entry["backend_overrides"] = _copy_policy(
        {
            key: value
            for key, value in backend.items()
            if key in _BACKEND_PUBLIC_FIELDS
        },
        _BACKEND_PUBLIC_FIELDS,
        f"models.{alias}.backend_overrides",
    )
    currencies[alias] = provider_currency_for_url(
        entry.get("url") or backend.get("completions_url") or inherited_url
    )
    return public_entry


def public_config_snapshot(config: dict[str, Any]) -> PublicConfigSnapshot:
    """Project a real worker config without filling defaults or muting features."""
    if not isinstance(config, dict):
        raise PublicConfigError("Expected flattened worker configuration")
    output = {}
    currencies = {}
    for key, value in config.items():
        if key in WORKER_ONLY_FIELDS:
            continue
        if key == "models":
            output[key] = _public_models(
                value, currencies, config.get("completions_url")
            )
            continue
        if key not in PUBLIC_POLICY_SCHEMA:
            raise PublicConfigError(f"Unclassified public config field: {key}")
        output[key] = _copy_policy(value, PUBLIC_POLICY_SCHEMA[key], key)
    if "os_backend" in output and output["os_backend"] not in (
        "local",
        "harbor",
    ):
        raise PublicConfigError("Invalid public backend kind")
    return PublicConfigSnapshot(
        output,
        bool(config.get("completions_url")),
        provider_currency_for_url(config.get("completions_url")),
        currencies,
    )
