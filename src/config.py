import os
import shutil
import sys
from pathlib import Path
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
from bundled_assets import bundled_root
from mcp_runtime.paths import managed_config_path

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


def resolve_tui_stream_enabled(config_dict: Dict[str, Any]) -> bool:
    """Мастер-свитч live-стриминга TUI: явный `tui_stream` (True/False) побеждает;
    иначе legacy top-level `stream` (backend.stream); иначе дефолт True.
    None у `tui_stream` = «не задан» → падаем на legacy, чтобы не затирать его."""
    tui_stream = config_dict.get("tui_stream")
    if tui_stream is not None:
        return bool(tui_stream)
    return bool(config_dict.get("stream", True))


def _default_config_path() -> str:
    # Floor: bundled дефолт-конфиг (едет в wheel через force-include).
    return os.path.join(str(bundled_root()), "config.default.yml")


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
    candidate = os.path.abspath(os.path.join(str(env_vars.effective_config_dir()), "config.yml"))
    if os.path.isfile(candidate):
        return candidate
    return None


def user_config_path() -> Path:
    """Путь дефолтного пользовательского конфига: <gc config>/config.yml.
    На залоченном ~/.config (корп-Mac/Linux) автоматически уезжает в ~/.gc."""
    return env_vars.effective_config_dir() / "config.yml"


def _bootstrap_user_config() -> bool:
    """Первый запуск без --config и без ~/.gc/config.yml: скопировать floor
    в ~/.gc/config.yml, чтобы у пользователя был редактируемый конфиг.

    Возвращает True, если файл создан сейчас (иначе уже существовал). Презентация
    (онбординг-гайд) — на стороне контроллеров (cli/tui), не здесь."""
    dest = user_config_path()
    if dest.is_file():
        return False
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_default_config_path(), dest)
    except OSError as exc:
        print(
            f"[gc] не удалось создать пользовательский конфиг {dest} ({exc}); "
            f"работаю на дефолте. Задайте GC_HOME на доступную папку или используйте --config.",
            file=sys.stderr,
        )
        return False
    return True


def resolve_base_config_path(config_path: Optional[str] = None) -> str:
    """Резолвить путь к base config.yml без загрузки YAML.

    Использует ту же логику что `load_config`: явный путь → XDG → пакетный
    дефолт. Нужен swarm-runner'у (см. шаг M4/1.10), чтобы материализовать
    runtime config.yml внутри песочницы на основании хостового base YAML.
    """
    return config_path or _xdg_config_path() or _default_config_path()


def apply_cli_overrides(
    config_dict: Dict[str, Any],
    *,
    extra_overrides: Optional[Dict[str, Any]] = None,
    write_guard_enabled: Optional[bool] = None,
    no_write_guard: bool = False,
    command_moderation_mode: Optional[str] = None,
    read_guard_enabled: Optional[bool] = None,
    no_read_guard: bool = False,
    read_guard_mode: Optional[str] = None,
    agentsmd_override: bool = False,
) -> None:
    if extra_overrides:
        config_dict.update(extra_overrides)
    effective_write_guard_override = False if no_write_guard else write_guard_enabled
    effective_read_guard_override = False if no_read_guard else read_guard_enabled
    apply_cli_command_moderation_overrides(
        config_dict,
        write_guard_enabled=effective_write_guard_override,
        command_moderation_mode=command_moderation_mode,
        read_guard_enabled=effective_read_guard_override,
        read_guard_mode=read_guard_mode,
    )
    if agentsmd_override:
        config_dict["agentsmd_override"] = True
    sync_logging_flat_and_params(config_dict)


def resolve_config_path(config_path: Optional[str] = None) -> str:
    """Путь config.yml, использованный сессией (та же цепочка, что в load_config)."""
    return config_path or _xdg_config_path() or _default_config_path()


def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    if config_path is None and _xdg_config_path() is None:
        _bootstrap_user_config()
    path = resolve_config_path(config_path)
    config_dir = os.path.dirname(os.path.abspath(path))

    with open(path, "r", encoding="utf-8") as file:
        raw_config = yaml.safe_load(file) or {}

    # Machine-managed overlay (gc-mcp install/remove): ~/.gc/config.local.yml.
    # Мёржится поверх base, под project-local. mcp.servers аккумулируются.
    managed_path = managed_config_path()
    if managed_path.is_file() and os.path.abspath(str(managed_path)) != os.path.abspath(path):
        with open(managed_path, "r", encoding="utf-8") as file:
            managed_raw = yaml.safe_load(file) or {}
        raw_config = deep_merge(raw_config, managed_raw)

    # Layered loading: project <cwd>/.gc/config.yml поверх глобального.
    project_path = _project_local_config_path()
    if project_path is not None and os.path.abspath(project_path) != os.path.abspath(path):
        with open(project_path, "r", encoding="utf-8") as file:
            project_raw = yaml.safe_load(file) or {}
        raw_config = deep_merge(raw_config, project_raw)

    app_cfg = AppConfig.model_validate(raw_config)
    resolve_paths(app_cfg, config_dir)

    # Устаревшие ключи, заданные юзером ЯВНО (pydantic model_fields_set знает
    # explicit-set vs дефолт). Голый факт → startup_notices форматирует текст.
    _deprecated_keys_used: list[str] = []
    if "stream" in app_cfg.backend.model_fields_set:
        _deprecated_keys_used.append("backend.stream")

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
        "stream": app_cfg.backend.stream,  # legacy: канонический ключ — tui.stream
        "deprecated_keys_used": _deprecated_keys_used,
        "tui_stream": app_cfg.tui.stream,
        "tui_stream_display": app_cfg.tui.stream_display,
        "tui_text_answer_mode": app_cfg.tui.text_answer_mode,
        "command_moderation": {
            "enabled": app_cfg.security.command_moderation.enabled,
            "mode": app_cfg.security.command_moderation.mode,
            "allowed_command_examples": app_cfg.security.command_moderation.allowed_command_examples,
        },
        "write_guard": {
            "enabled": app_cfg.security.write_guard.enabled,
            "mode": app_cfg.security.write_guard.mode,
            "extra_paths": app_cfg.security.write_guard.extra_paths,
        },
        "read_guard": app_cfg.security.read_guard.model_dump(),
        "url_policy": {
            "enabled": app_cfg.security.url_policy.enabled,
            "mode": app_cfg.security.url_policy.mode,
            "allowed_schemes": app_cfg.security.url_policy.allowed_schemes,
            "url_blacklist": app_cfg.security.url_policy.url_blacklist,
        },
        "download": app_cfg.security.download.model_dump(),
        "summary_params": app_cfg.summary.model_dump(),
        "chat_completions_params": app_cfg.chat_completions.model_dump(),
        "chat_completions_llm": app_cfg.chat_completions.llm,
        "quality_gate_params": app_cfg.quality_gate.model_dump(),
        # Limits for ``session_memory`` tool; yaml/config keys stay ``working_memory*``.
        "working_memory": app_cfg.working_memory,
        "working_memory_params": app_cfg.working_memory.model_dump(),
        "working_memory_max_chars_goal": app_cfg.working_memory.max_chars_goal,
        "working_memory_max_chars_constraints": app_cfg.working_memory.max_chars_constraints,
        "working_memory_max_chars_state": app_cfg.working_memory.max_chars_state,
        "working_memory_max_chars_knowledge": app_cfg.working_memory.max_chars_knowledge,
        "working_memory_total_budget_chars": app_cfg.working_memory.total_budget_chars,
        "tool_usage_notice": app_cfg.tool_usage_notice,
        "tool_usage_notice_params": [
            rule.model_dump() for rule in app_cfg.tool_usage_notice
        ],
        "project_memory": app_cfg.project_memory,
        "project_memory_params": app_cfg.project_memory.model_dump(),
        "project_memory_check_interval_messages": app_cfg.project_memory.check_interval_messages,
        "project_memory_max_chars": app_cfg.project_memory.max_chars,
        "project_memory_max_append_chars": app_cfg.project_memory.max_append_chars,
        "enable_project_memory_notice": app_cfg.project_memory.enable_project_memory_notice,
        "intent_scope": app_cfg.intent_scope,
        "intent_scope_params": app_cfg.intent_scope.model_dump(),
        "logging_params": app_cfg.logging.model_dump(),
        "agent_md_path": app_cfg.paths.agent_md_path,
        "chat_completions_system_prompt_path": app_cfg.paths.chat_completions_system_prompt_path,
        "chat_completions_intent_guard_prompt_path": app_cfg.paths.chat_completions_intent_guard_prompt_path,
        "chat_completions_special_instructions_path": app_cfg.paths.chat_completions_special_instructions_path,
        "threads_data_path": app_cfg.paths.threads_data_path,
        "skills_data_path": app_cfg.paths.skills_data_path,
        "logs_dump_dir": app_cfg.paths.logs_dump_dir,
        "agentsmd_override": app_cfg.tui.agentsmd_override,
        "model": app_cfg.agent.model,
        "system_prompt_id": app_cfg.agent.system_prompt_id,
        "use_server_subagents": app_cfg.agent.use_server_subagents,
        "use_local_subagents": app_cfg.agent.use_local_subagents,
        "subagent_max_steps": app_cfg.agent.subagent_max_steps,
        "client_name": app_cfg.agent.client_name,
        "x_cl_id": app_cfg.agent.x_cl_id,
        "verbose": app_cfg.agent.verbose,
        "stop_tools": app_cfg.agent.stop_tools,
        "disable_skills": app_cfg.agent.disable_skills,
        "server_tool_prefix": app_cfg.agent.server_tool_prefix,
        "max_steps": app_cfg.agent.max_steps,
        "no_tool_call_max_count": app_cfg.agent.no_tool_call_max_count,
        "strict_turn_finish_validation": app_cfg.agent.strict_turn_finish_validation,
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
        "pricing": app_cfg.pricing.model_dump(),
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
