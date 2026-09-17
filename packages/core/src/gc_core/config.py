import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import urllib3

from gc_core.config_modules.auth import build_auth_connector
from pydantic import ValidationError as _PydanticValidationError

from gc_core.config_modules.env_vars import project_gc_dir
from gc_core.config_modules.backend import parse_chat_completions_max_tokens  # noqa: F401  # re-export: processing_responses импортирует из config
from gc_core.config_modules.common import (
    apply_swarm_env_overrides,
)
from gc_core.config_modules.env_interpolation import resolve_env_placeholders
from gc_core.config_modules.root import (AppConfig, SCHEMA_ERROR_PREFIX, resolve_paths,
                                 schema_error_details)
from gc_core.config_modules.security import (
    apply_cli_command_moderation_overrides,
)
from gc_core.config_modules.compat import apply_compat
from gc_core.config_modules.model_registry import ModelEntry, validate_registry_pairs
from gc_core.config_modules.model_sections_validation import validate_model_sections
from gc_core.config_modules.profiles import (
    ProfileMeta,
    mtime_or_none,
    profile_roots,
    validate_profile_text,
)
from gc_core.config_modules.summary import DEFAULT_LAST_DIALOG_STEP_MESSAGE_MAX_CHARS
from gc_core.config_modules import env_vars
from gc_core.bundled_assets import bundled_root
# managed_config_path живёт в config_modules.env_vars, а не в mcp_runtime.paths:
# путь принадлежит конфигу, и держать его в MCP-подсистеме значило бы вернуть
# статическую зависимость ядра конфига от MCP, снятую при разбиении на пакеты.
from gc_core.config_modules import env_vars as mcp_paths
# Наложение слоёв и pre-write валидация живут в `config_modules.layers` (слой записи
# реестра спрашивает их напрямую, минуя агрегатор). Здесь — ре-экспорт: снаружи их
# зовут как `config.<имя>`, и второй адрес у одного правила заводить незачем.
from gc_core.config_modules.layers import (  # noqa: F401
    _Layers,
    _layer_document,
    validate_config_text,
    _merge_layers,
    _raw_from_text,
    _app_config_from_raw,
    _app_config_from_text,
    _stacked_app_config,
    prospective_registry,
    resolve_profile_pointer,
    stack_documents,
    validate_config_text_with_sections,
    validate_profile_text_with_sections,
)

from gc_core.trace_id import normalize_trace_id
from gc_core.utils.user_text import unreadable_reason

# Backward compatibility for existing imports.
_DEFAULT_LAST_DIALOG_STEP_MESSAGE_MAX_CHARS = (
    DEFAULT_LAST_DIALOG_STEP_MESSAGE_MAX_CHARS
)

_LOGGING_SYNC_KEYS = (
    "full_log_backend",
    "console_trace",
    "save_logs",
    "logging_format",
    "log_level",
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
    trace_id: Optional[str] = None,
) -> None:
    if extra_overrides:
        resolved = resolve_env_placeholders(extra_overrides)
        config_dict.update(resolved)
        if "models" in resolved:
            # `--models` задаёт весь реестр строкой и идёт мимо схемы (плоский
            # config_dict заполняется уже после валидации), поэтому здесь руками
            # повторяется то, что файловому пути делает flatten_app_config: правило
            # пары и приведение записей к СЕРИАЛИЗОВАННОЙ форме. Без второго Router
            # читал бы сырой словарь, где нет `authorization_header`, и `token` из
            # CLI молча пропадал бы — запись уходила на свой адрес с общим ключом.
            validate_registry_pairs({"models": resolved["models"]}, source="--models")
            if isinstance(resolved["models"], dict):
                config_dict["models"] = {
                    alias: _serialize_model_entry(ModelEntry.model_validate(entry))
                    for alias, entry in resolved["models"].items()
                }
        # Признак «ключ пришёл именно из CLI» (спека §2): нужен, среди прочего,
        # для детекта легаси-форса (см. cli_forces_legacy_model ниже).
        explicit = set(config_dict.get("cli_explicit_keys") or ())
        config_dict["cli_explicit_keys"] = sorted(explicit | set(resolved))
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
    # Внешний trace_id: значение могло приехать и настоящим флагом, и generic
    # `--param`-оверрайдом (там оно прошло через yaml.safe_load и может быть не
    # строкой). Нормализуем в одной точке; пусто → ключа нет вовсе, чтобы
    # «отсутствует» было отсутствием ключа, а не пустой строкой.
    if trace_id is not None:
        config_dict["trace_id"] = trace_id
    normalized_trace_id = normalize_trace_id(config_dict.get("trace_id"))
    if normalized_trace_id:
        config_dict["trace_id"] = normalized_trace_id
    else:
        config_dict.pop("trace_id", None)
    sync_logging_flat_and_params(config_dict)


def cli_forces_legacy_model(config_dict: Dict[str, Any]) -> bool:
    """Легаси-форс (спека §2, §12.1): CLI явно передал `model`/`completions_url`
    поверх непустого merged-реестра моделей — резолв уровней/алиасов у
    ``ModelRouter`` отключается на этот запуск (``force_legacy=True``).
    Профиль-слой обычных секций (гварды, суммаризация и пр.) при этом остаётся
    в стопке — это решает исключительно резолв модели, не сборку конфига.
    ``--model``/``--completions_url`` при пустом реестре — обычный легаси, без форса.
    """
    explicit = set(config_dict.get("cli_explicit_keys") or ())
    if not explicit & {"model", "completions_url"}:
        return False
    return bool(config_dict.get("models"))


def resolve_config_path(config_path: Optional[str] = None) -> str:
    """Путь config.yml, использованный сессией (та же цепочка, что в load_config)."""
    return config_path or _xdg_config_path() or _default_config_path()


def _serialize_model_entry(entry: ModelEntry) -> Dict[str, Any]:
    """Проекция ``ModelEntry`` для ``ModelRouter`` (src/model_router.py).

    ``authorization_header`` — уже Bearer-нормализованное значение (None=наследовать,
    ""=явно без, иначе значение) — потребителю нужен именно computed-результат, не
    сырой ``token``. ``backend_overrides`` — только явно заданные ключи
    (``exclude_unset``), чтобы Router накладывал поверх эффективного backend лишь то,
    что модель реально переопределяет, а не дефолты ``BackendConfig``.
    """
    return {
        "model": entry.model,
        "url": entry.url,
        "authorization_header": entry.authorization_header,
        "pricing": entry.pricing.model_dump() if entry.pricing is not None else None,
        "backend_overrides": (
            entry.backend.model_dump(exclude_unset=True) if entry.backend is not None else {}
        ),
    }


def flatten_app_config(app_cfg: AppConfig) -> Dict[str, Any]:
    """Проекция валидированного AppConfig в плоский config_dict агентного слоя.

    Единственное место, где схема раскладывается в плоские ключи. Любой сборщик
    конфига (load_config, harbor-адаптер) обязан идти через неё, иначе новый ключ
    схемы не доедет до потребителей, которые читают его как cfg["..."].
    """
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
        "send_tool_strict": app_cfg.backend.send_tool_strict,
        "claude_prompt_cache": app_cfg.backend.claude_prompt_cache,
        "use_ssl": app_cfg.backend.use_ssl,
        "cert_path": app_cfg.backend.cert_path,
        "key_path": app_cfg.backend.key_path,
        "use_retry": app_cfg.backend.use_retry,
        "retry_embedded_choice_errors": app_cfg.backend.retry_embedded_choice_errors,
        "backend_request_timeout_sec": app_cfg.backend.request_timeout_sec,
        "proxy": app_cfg.backend.proxy,
        "stream": app_cfg.backend.stream,  # legacy: канонический ключ — tui.stream
        "deprecated_keys_used": _deprecated_keys_used,
        "tui_stream": app_cfg.tui.stream,
        "tui_stream_display": app_cfg.tui.stream_display,
        "tui_text_answer_mode": app_cfg.tui.text_answer_mode,
        "tui_finish_gate_mode": app_cfg.tui.finish_gate_mode,
        "tui_finish_gate_mode_explicit": "finish_gate_mode" in app_cfg.tui.model_fields_set,
        "strict_turn_finish_explicit_false": (
            "strict_turn_finish_validation" in app_cfg.agent.model_fields_set
            and not app_cfg.agent.strict_turn_finish_validation
        ),
        "update": app_cfg.update.model_dump(),  # спека D: check/extra_args
        "tui_bang_shell": app_cfg.tui.bang_shell.model_dump(),
        "tui_file_view": app_cfg.tui.file_view.model_dump(),
        "voice": {
            "provider": app_cfg.voice.provider,
            "url": app_cfg.voice.url,
            "token": app_cfg.voice.token,
            "model": app_cfg.voice.model,
            "language": app_cfg.voice.language,
            "max_seconds": app_cfg.voice.max_seconds,
            "timeout_seconds": app_cfg.voice.timeout_seconds,
            "verify_ssl": app_cfg.voice.verify_ssl,
        },
        "endpoint_presets": [p.model_dump() for p in app_cfg.endpoint_presets],
        "voice_endpoint_presets": [p.model_dump() for p in app_cfg.voice_endpoint_presets],
        "command_moderation": {
            "enabled": app_cfg.security.command_moderation.enabled,
            "mode": app_cfg.security.command_moderation.mode,
            "allowed_command_examples": app_cfg.security.command_moderation.allowed_command_examples,
            "denied_command_examples": app_cfg.security.command_moderation.denied_command_examples,
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
        # Плоский dict рядом с chat_completion_kwargs: deep_merge (бенч/project)
        # обновляет его; живой pydantic-объект сюда не кладём — иначе early-return
        # в auxiliary_llm_from_config_dict игнорирует overlay.
        "auxiliary_llm_params": app_cfg.chat_completions.auxiliary_llm.model_dump(),
        "quality_gate_params": app_cfg.quality_gate.model_dump(),
        # Limits for ``session_memory`` tool; yaml/config keys stay ``working_memory*``.
        "working_memory": app_cfg.working_memory,
        "working_memory_params": app_cfg.working_memory.model_dump(),
        "todo_params": app_cfg.todo.model_dump(),
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
        "chat_completions_custom_subagent_system_prompt_path": app_cfg.paths.chat_completions_custom_subagent_system_prompt_path,
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
        "subagents_dir": app_cfg.agent.subagents_dir,
        "subagents_max_count": app_cfg.agent.subagents_max_count,
        "client_name": app_cfg.agent.client_name,
        "x_cl_id": app_cfg.agent.x_cl_id,
        "trace_id_header_name": app_cfg.agent.trace_id_header_name,
        "trace_header_enabled": app_cfg.agent.trace_header_enabled,
        "verbose": app_cfg.agent.verbose,
        "stop_tools": app_cfg.agent.stop_tools,
        "disable_skills": app_cfg.agent.disable_skills,
        "server_tool_prefix": app_cfg.agent.server_tool_prefix,
        "max_steps": app_cfg.agent.max_steps,
        "session_token_limit": app_cfg.agent.session_token_limit,
        "max_tool_calls": app_cfg.agent.max_tool_calls,
        "no_tool_call_max_count": app_cfg.agent.no_tool_call_max_count,
        "max_unknown_tool_repeats": app_cfg.agent.max_unknown_tool_repeats,
        "strict_turn_finish_validation": app_cfg.agent.strict_turn_finish_validation,
        "finish_gate_max_iterations": app_cfg.agent.finish_gate_max_iterations,
        "subagent_finish_gate_max_iterations": app_cfg.agent.subagent_finish_gate_max_iterations,
        "finish_gate_use_rule_hints": app_cfg.agent.finish_gate_use_rule_hints,
        "console_trace": app_cfg.logging.console_trace,
        "save_logs": app_cfg.logging.save_logs,
        "logging_format": app_cfg.logging.format,
        "log_level": app_cfg.logging.log_level,
        "redact_secrets": app_cfg.logging.redact_secrets.model_dump(),
        "limit_project_metadata": app_cfg.agent.limit_project_metadata,
        "tool_output_inline_max_chars": app_cfg.agent.tool_output_inline_max_chars,
        "stored_tool_outputs_max_entries": app_cfg.agent.stored_tool_outputs_max_entries,
        "exec_timeout_sec": app_cfg.agent.exec_timeout_sec,
        "exec_output_capture_max_chars": app_cfg.agent.exec_output_capture_max_chars,
        "exec_output_preview_edge_chars": app_cfg.agent.exec_output_preview_edge_chars,
        "walk_output_max_chars": app_cfg.agent.walk_output_max_chars,
        "harbor_exec_init": app_cfg.agent.harbor_exec_init,
        "inject_tool_fields": dict(app_cfg.agent.inject_tool_fields),
        "model_steering": app_cfg.agent.model_steering,
        "limit_trace_summary": app_cfg.agent.limit_trace_summary,
        "loop_detector": app_cfg.agent.loop_detector.model_dump(),
        "mcp-servers": {
            name: server.model_dump(by_alias=True) for name, server in app_cfg.mcp.servers.items()
        },
        "mcp-meta": app_cfg.mcp.meta.model_dump(by_alias=True),
        "pricing": app_cfg.pricing.model_dump(),
        "agent_auth": app_cfg.agent_auth.model_dump() if app_cfg.agent_auth else {},
        "diff_tui": app_cfg.diff_tui.model_dump(),
        "fs_tools": app_cfg.fs_tools.model_dump(),
        "strip_tags_projection": app_cfg.strip_tags_projection.model_dump(),
        # Секции моделей (спека §4/§5) — потребитель: ModelRouter (src/model_router.py).
        # Router работает от этого снимка, а не от app_cfg напрямую (§6.4).
        "models": {
            alias: _serialize_model_entry(entry) for alias, entry in app_cfg.models.items()
        },
        "slots": app_cfg.slots.model_dump(),
        "assign": app_cfg.assign.model_dump(),
        "profile": app_cfg.profile,
    }

    return config_dict


def _build_mtime_set(
    base_path: Path,
    managed_path: Path,
    project_path: Optional[str],
    active_name: Optional[str],
    profile_roots_list: list[Path],
) -> Dict[str, Optional[float]]:
    """Снимок mtime базового/managed/проектного конфига + обоих кандидатных путей
    активного профиля (спека §3.3): отсутствующий файл — ключ есть, значение None."""
    project_config_path = Path(project_path) if project_path is not None else project_gc_dir() / "config.yml"
    mtime_set: Dict[str, Optional[float]] = {
        str(base_path): mtime_or_none(base_path),
        str(managed_path): mtime_or_none(managed_path),
        str(project_config_path): mtime_or_none(project_config_path),
    }
    if active_name:
        for root in profile_roots_list:
            candidate = Path(root) / f"{active_name}.yml"
            mtime_set[str(candidate)] = mtime_or_none(candidate)
    return mtime_set


def profile_mtime_set(
    config_path: Optional[str], active_name: Optional[str] = None
) -> Dict[str, Optional[float]]:
    """Тот же снимок, что строит `load_config_ex` (спека §3.3), но БЕЗ парсинга/
    валидации файлов — только `stat()`. Точка входа для TUI (`set_config_path`):
    связывание пути с окном не обязано гонять полную загрузку конфига ради снимка
    свежести."""
    path = resolve_config_path(config_path)
    base_path = Path(path)
    # Через модуль-дом, а не через импортированные сюда имена: адрес чтения
    # один на весь загрузчик (`_merge_layers` спрашивает там же), и подмена
    # дома в тестах действует на обе точки разом.
    managed_path = mcp_paths.managed_config_path()
    project_path = env_vars.project_local_config_path()
    roots = profile_roots(Path(os.getcwd()), base_path)
    return _build_mtime_set(base_path, managed_path, project_path, active_name, roots)


def load_config_ex(
    config_path: Optional[str] = None,
    *,
    profile_override: Optional[str] = None,
    apply_to_process: bool = True,
) -> tuple[Dict[str, Any], ProfileMeta]:
    """Загрузить эффективный конфиг + метаданные резолва профиля (спека §2, §3).

    Слои: base → managed → project (deep-merge, каждый слой интерполируется
    ${ENV:VAR} сразу после чтения) → указатель `profile:` резолвится из этого мержа
    (либо `profile_override`) → контент профиля мержится ПОВЕРХ → compat →
    `AppConfig.model_validate` → `validate_model_sections` → resolve_paths/flatten.
    `load_config` (ниже) — тонкая обёртка для обратной совместимости.

    `apply_to_process=False` — собрать конфиг, НЕ применяя его к процессу. Сбор
    сам по себе не безобиден: он глушит предупреждения TLS — эффект глобальный
    и необратимый. Тому, кто конфиг ПРИМЕНЯЕТ (старт, reload, выбор профиля в
    сессии), это и нужно; тому, кто его ПОКАЗЫВАЕТ (витрина чужого профиля в
    Настройках), — нет: заглянуть в соседний профиль не должно менять того, как
    работающий агент проверяет сертификаты."""
    if config_path is None and _xdg_config_path() is None:
        _bootstrap_user_config()
    path = resolve_config_path(config_path)
    config_dir = os.path.dirname(os.path.abspath(path))
    base_path = Path(path)
    notices: list[str] = []

    # Чтение базы — тем же приёмом, что и слои (`_layer_document`): невалидный YAML и
    # не-словарь на верхнем уровне дают ValueError с именем файла и причиной, а не
    # сырые yaml.YAMLError/AttributeError из глубины мержа. Голый open() снаружи
    # `_layer_document` — сам он не оборачивает OSError/UnicodeDecodeError (см. его
    # докстринг), это остаётся на вызывающем; здесь единственный вызывающий, которому
    # нужен непрочитанный файл тем же ValueError, что и остальные исходы базы.
    # `UnicodeDecodeError` — подкласс `ValueError`, а не `OSError` (файл, сохранённый
    # «блокнотом» в UTF-16), и без отдельного перехвата уходил голой жалобой кодека
    # мимо этой обёртки и мимо классового сторожа (ревью MR #80, L40).
    try:
        raw_config = _layer_document(base_path, "config.yml")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(unreadable_reason(base_path, exc)) from exc
    # Дорезолвный вид держим для отказа правила пары: после подстановки от `${ENV:X}`
    # остаётся пустая строка, и назвать незаданную переменную уже нечем.
    base_before_env = raw_config
    raw_config = resolve_env_placeholders(raw_config)
    validate_registry_pairs(raw_config, source=str(base_path),
                            raw_source=base_before_env)

    (raw_config, ref, active_name, roots, managed_path, project_path,
     managed_raw, _base_voice) = _merge_layers(
        raw_config, path, profile_override=profile_override, notices=notices,
        # Пути слоёв добывает ЗАГРУЗЧИК — своими именами, теми же, что зовёт
        # `profile_mtime_set` ниже и что подменяют тесты, глуша слои машины
        # разработчика. Спроси их `_merge_layers` сам у модулей-владельцев —
        # подмена бы не действовала, и «изолированный» тест читал бы реальный
        # `~/.gc/config.local.yml` и проектный `.gc/config.yml`.
        managed_path=mcp_paths.managed_config_path(),
        project_path=env_vars.project_local_config_path(),
    )

    mtime_set = _build_mtime_set(base_path, managed_path, project_path, active_name, roots)

    # Перекрыт ли юзерский voice managed-слоем (gc-voice install) — для notices. Правило
    # общее с установкой: она предупреждает о том же факте, и вердикт обязан совпадать.
    #
    # Считаем ПОСЛЕ ВСЕХ слоёв и только когда адрес батарейки реально победил. Сперва
    # расчёт стоял между managed и project — и агент предупреждал «managed перекрыл ваш
    # voice» там, где работал адрес из <cwd>/.gc/config.yml (находка ревью коллеги).
    # Профиль — такой же слой поверх: если voice.url задан в нём, батарейка тоже ничего
    # не решает, поэтому место расчёта — ниже профильного мержа, а не выше.
    _voice_overridden: Optional[str] = None
    _mv = managed_raw.get("voice") or {}
    if _mv:
        from gc_core.config_modules.voice import overridden_user_voice
        _managed_url = str(_mv.get("url") or "")
        _merged_voice = raw_config.get("voice")
        _effective_url = str((_merged_voice or {}).get("url") or "") \
            if isinstance(_merged_voice, dict) else ""
        if _effective_url == _managed_url:
            _voice_overridden = overridden_user_voice(_base_voice, _managed_url)

    # Compat-слой (A4): rename/remove устаревших ключей in-memory, файл не трогаем.
    # Применяется к итоговому мержу — профильный слой проходит его так же, как
    # остальные (единый вызов после всех merge-слоёв, включая профиль).
    _compat_notices = apply_compat(raw_config)

    # Отказ схемы — из `exc.errors()`, не `str(exc)`: pydantic-дамп несёт
    # `input_value=<repr записи>` — токен из файла уезжал в хинт пикера профилей
    # и в статус «не применится» дословным текстом ошибки (MR #80, класс L28).
    try:
        app_cfg = AppConfig.model_validate(raw_config)
    except _PydanticValidationError as exc:
        raise ValueError(
            f"{SCHEMA_ERROR_PREFIX}{schema_error_details(exc)}") from exc
    _model_section_notices = validate_model_sections(app_cfg)
    resolve_paths(app_cfg, config_dir)

    config_dict = flatten_app_config(app_cfg)
    config_dict["level"] = app_cfg.level
    # Плоский `profile` — ДЕЙСТВУЮЩИЙ профиль, а не указатель из файлов: документу
    # профиля запрещено нести ключ `profile:`, поэтому после мержа app_cfg.profile
    # остаётся пойнтером config.yml и при `--profile other` (или сессионном выборе,
    # §9.1) врал бы витринам — `overriding_layer_note` называл бы чужое имя.
    config_dict["profile"] = active_name or ""
    # Не поле схемы: результат разбора СЫРОГО конфига compat-слоем — докладывает загрузчик.
    # Фаза 2 секций моделей (ссылочная целостность на merged-реестре) и лоадер
    # профилей (зарезервированные имена, секрет-политика) — тот же канал notices.
    config_dict["compat_notices"] = _compat_notices + _model_section_notices + notices
    # Не поле схемы: факт перекрытия юзерского voice managed-слоем — знает только загрузчик.
    config_dict["voice_overridden_by_managed"] = _voice_overridden

    apply_swarm_env_overrides(config_dict, config_dir)

    sync_logging_flat_and_params(config_dict)

    verify = config_dict["use_ssl"]
    if verify is False and apply_to_process:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    meta = ProfileMeta(active_name=active_name, ref=ref, mtime_set=mtime_set, notices=notices)
    return config_dict, meta


def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    config_dict, _meta = load_config_ex(config_path)
    return config_dict


if __name__ == "__main__":
    cfg = load_config()
    print(cfg)
    auth = build_auth_connector(cfg)
    if auth:
        print(auth.get_token())
