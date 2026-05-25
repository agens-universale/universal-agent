import os
from typing import Any, Dict, Optional

import urllib3
import yaml

from config_modules.auth import build_auth_connector
from config_modules.env_vars import project_gc_dir
from config_modules.backend import parse_chat_completions_max_tokens
from config_modules.common import (
    apply_swarm_env_overrides,
)
from config_modules.merge import deep_merge
from config_modules.root import AppConfig, resolve_paths
from config_modules.security import (
    apply_cli_command_moderation_overrides,
)
from config_modules.summary import DEFAULT_LAST_DIALOG_STEP_MESSAGE_MAX_CHARS
from config_modules import env_vars

# Backward compatibility for existing imports.
_DEFAULT_LAST_DIALOG_STEP_MESSAGE_MAX_CHARS = (
    DEFAULT_LAST_DIALOG_STEP_MESSAGE_MAX_CHARS
)

_LOGGING_SYNC_KEYS = (
    "full_log_backend",
    "console_trace",
    "dump_storage",
    "save_logs",
)


def sync_logging_flat_and_params(config_dict: Dict[str, Any]) -> None:
    """Согласовать logging_params и плоские ключи (после merge overrides и т.п.)."""
    lp = dict(config_dict.get("logging_params") or {})
    for key in _LOGGING_SYNC_KEYS:
        if key in config_dict:
            lp[key] = config_dict[key]
    config_dict["logging_params"] = lp
    for key in _LOGGING_SYNC_KEYS:
        if key in lp:
            config_dict[key] = lp[key]


def _default_config_path() -> str:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, "..", "config.yml")


def _project_local_config_path() -> Optional[str]:
    """
    Вернуть абсолютный путь к <cwd>/.gc/config.yml, если файл существует, иначе None.

    Используется для layered loading: project local config мерджится поверх глобального.
    """
    candidate = str((project_gc_dir() / "config.yml").resolve())
    if os.path.isfile(candidate):
        return candidate
    return None


def _xdg_config_path() -> Optional[str]:
    """
    Вернуть абсолютный путь к <gc_paths()["config"]>/config.yml, если файл
    существует, иначе None.

    Базовый tier между --config и пакетным дефолтом. Каталог берётся из
    gc_paths()["config"] (XDG-config: ~/.config/gc/ на Linux/macOS,
    %USERPROFILE%\\.gc\\ на Windows; учитывает GC_CONFIG_DIR / XDG_CONFIG_HOME
    / GC_HOME). Полная база-замена, НЕ слой: частичный файл здесь теряет
    пакетные дефолты (project-local <cwd>/.gc/config.yml по-прежнему
    мерджится поверх выбранной базы).
    """
    candidate = os.path.abspath(str(env_vars.gc_paths()["config"] / "config.yml"))
    if os.path.isfile(candidate):
        return candidate
    return None


def apply_cli_overrides(
    config_dict: Dict[str, Any],
    *,
    extra_overrides: Optional[Dict[str, Any]] = None,
    root_box_enabled: Optional[bool] = None,
    no_root_box: bool = False,
    command_moderation_mode: Optional[str] = None,
) -> None:
    if extra_overrides:
        config_dict.update(extra_overrides)
    effective_root_box_override = False if no_root_box else root_box_enabled
    apply_cli_command_moderation_overrides(
        config_dict,
        root_box_enabled=effective_root_box_override,
        command_moderation_mode=command_moderation_mode,
    )
    sync_logging_flat_and_params(config_dict)


def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    path = config_path or _xdg_config_path() or _default_config_path()
    config_dir = os.path.dirname(os.path.abspath(path))

    with open(path, "r", encoding="utf-8") as file:
        raw_config = yaml.safe_load(file) or {}

    # Layered loading: project <cwd>/.gc/config.yml поверх глобального.
    project_path = _project_local_config_path()
    if project_path is not None and os.path.abspath(project_path) != os.path.abspath(path):
        with open(project_path, "r", encoding="utf-8") as file:
            project_raw = yaml.safe_load(file) or {}
        raw_config = deep_merge(raw_config, project_raw)

    app_cfg = AppConfig.model_validate(raw_config)
    resolve_paths(app_cfg, config_dir)

    config_dict: Dict[str, Any] = {
        "backend_api_mode": app_cfg.backend.api_mode,
        "completions_url": app_cfg.backend.completions_url,
        "chat_completions_max_tokens": app_cfg.backend.chat_completions_max_tokens,
        "chat_completions_min_response_tokens": app_cfg.backend.chat_completions_min_response_tokens,
        "chat_completion_headers": app_cfg.backend.chat_completion_headers,
        "chat_completion_kwargs": app_cfg.backend.chat_completion_kwargs,
        "use_max_tokens_param": app_cfg.backend.use_max_tokens_param,
        "openai_strict_tools_schema": app_cfg.backend.openai_strict_tools_schema,
        "use_ssl": app_cfg.backend.use_ssl,
        "use_retry": app_cfg.backend.use_retry,
        "command_moderation": {
            "mode": app_cfg.security.command_moderation.mode,
            "allowed_command_examples": app_cfg.security.command_moderation.allowed_command_examples,
            "root_box": {
                "enabled": app_cfg.security.command_moderation.root_box.enabled,
                "extra_write_paths": app_cfg.security.command_moderation.root_box.extra_write_paths,
            },
            "url_policy": {
                "allowed_schemes": app_cfg.security.command_moderation.url_policy.allowed_schemes,
                "url_blacklist": app_cfg.security.command_moderation.url_policy.url_blacklist,
            },
        },
        "summary_params": app_cfg.summary.model_dump(),
        "chat_completions_params": app_cfg.chat_completions.model_dump(),
        "chat_completions_llm": app_cfg.chat_completions.llm,
        "quality_gate_params": app_cfg.quality_gate.model_dump(),
        "memory_files_params": app_cfg.memory_files.model_dump(),
        "memory_files_max_file_chars": app_cfg.memory_files.max_file_chars,
        "memory_files_total_budget_chars": app_cfg.memory_files.total_budget_chars,
        "memory_files_max_files": app_cfg.memory_files.max_files,
        "memory_gate": app_cfg.memory_gate,
        "memory_gate_params": app_cfg.memory_gate.model_dump(),
        "memory_gate_cooldown_messages": app_cfg.memory_gate.cooldown_messages,
        "enable_memory_file_notice": app_cfg.memory_gate.enable_memory_file_notice,
        "long_memory": app_cfg.long_memory,
        "long_memory_params": app_cfg.long_memory.model_dump(),
        "long_memory_check_interval_messages": app_cfg.long_memory.check_interval_messages,
        "enable_long_memory_notice": app_cfg.long_memory.enable_long_memory_notice,
        "intent_scope": app_cfg.intent_scope,
        "intent_scope_params": app_cfg.intent_scope.model_dump(),
        "logging_params": app_cfg.logging.model_dump(),
        "agent_md_path": app_cfg.paths.agent_md_path,
        "chat_completions_system_prompt_path": app_cfg.paths.chat_completions_system_prompt_path,
        "threads_data_path": app_cfg.paths.threads_data_path,
        "skills_data_path": app_cfg.paths.skills_data_path,
        "logs_dump_dir": app_cfg.paths.logs_dump_dir,
        "model": app_cfg.agent.model,
        "system_prompt_id": app_cfg.agent.system_prompt_id,
        "use_server_subagents": app_cfg.agent.use_server_subagents,
        "use_local_subagents": app_cfg.agent.use_local_subagents,
        "subagent_max_steps": app_cfg.agent.subagent_max_steps,
        "client_name": app_cfg.agent.client_name,
        "x_cl_id": app_cfg.agent.x_cl_id,
        "verbose": app_cfg.agent.verbose,
        "stop_tools": app_cfg.agent.stop_tools,
        "server_tool_prefix": app_cfg.agent.server_tool_prefix,
        "max_steps": app_cfg.agent.max_steps,
        "no_tool_call_max_count": app_cfg.agent.no_tool_call_max_count,
        "strict_submit_solution_validation": app_cfg.agent.strict_submit_solution_validation,
        "finish_gate_max_iterations": app_cfg.agent.finish_gate_max_iterations,
        "subagent_finish_gate_max_iterations": app_cfg.agent.subagent_finish_gate_max_iterations,
        "finish_gate_use_rule_hints": app_cfg.agent.finish_gate_use_rule_hints,
        "console_trace": app_cfg.logging.console_trace,
        "dump_storage": app_cfg.logging.dump_storage,
        "save_logs": app_cfg.logging.save_logs,
        "limit_project_metadata": app_cfg.agent.limit_project_metadata,
        "tool_output_inline_max_chars": app_cfg.agent.tool_output_inline_max_chars,
        "stored_tool_outputs_max_entries": app_cfg.agent.stored_tool_outputs_max_entries,
        "inject_tool_fields": dict(app_cfg.agent.inject_tool_fields),
        "mcp-servers": {
            name: server.model_dump(by_alias=True) for name, server in app_cfg.mcp.servers.items()
        },
        "mcp-meta": app_cfg.mcp.meta.model_dump(by_alias=True),
        "agent_auth": app_cfg.agent_auth.model_dump() if app_cfg.agent_auth else {},
    }

    apply_swarm_env_overrides(config_dict, config_dir)

    sync_logging_flat_and_params(config_dict)

    verify = config_dict["use_ssl"]
    if verify is False:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    return config_dict


if __name__ == "__main__":
    cfg = load_config()
    print(cfg)
    auth = build_auth_connector(cfg)
    if auth:
        print(auth.get_token())
