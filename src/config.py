import os
from typing import Optional, Dict, Any
from uuid import uuid4

import urllib3
import yaml

from utils.prod_auth import AuthConnector


_DEFAULT_LAST_DIALOG_STEPS_COUNT = 5
_DEFAULT_LAST_DIALOG_STEP_MESSAGE_MAX_CHARS = 5000
_DEFAULT_MAX_MESSAGE_CHARS = 50000
_DEFAULT_MAX_DIALOGUE_CHARS = 1000000
_DEFAULT_MAX_ACCUMULATED_SUMMARY_CHARS = 20000
_DEFAULT_ORIGINAL_USER_MESSAGES_MAX_CHARS = 20000
_DEFAULT_INCREMENTAL_BATCH_MULTIPLIER = 1.4


def _default_config_path() -> str:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, "..", "config.yml")


def _resolve_path_from_config_dir(path_value: str, config_dir: str) -> str:
    if os.path.isabs(path_value):
        return path_value
    return os.path.join(config_dir, path_value)


def _maybe_override_and_resolve(
    maybe_env_value: Optional[str], key: str, config_dict: Dict[str, Any], config_dir: str
) -> None:
    if not isinstance(maybe_env_value, str):
        return
    env_value = maybe_env_value.strip()
    if not env_value:
        return
    config_dict[key] = _resolve_path_from_config_dir(env_value, config_dir)


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_chat_completions_max_tokens(raw: Any, *, default: int = 8096) -> int:
    """
    Значение chat_completions_max_tokens из YAML: int, float или строки вида "16k".
    Булевы и прочий мусор → default; ноль и отрицательные → default.
    """
    if raw is None:
        return default
    if isinstance(raw, bool):
        return default
    if isinstance(raw, (int, float)):
        n = int(raw)
        return n if n >= 1 else default
    if isinstance(raw, str):
        s = raw.strip().lower().replace(" ", "").replace("_", "")
        if not s:
            return default
        if s.endswith("k"):
            try:
                base = float(s[:-1])
                n = int(base * 1000)
            except ValueError:
                return default
            return n if n >= 1 else default
        try:
            n = int(float(s))
        except ValueError:
            return default
        return n if n >= 1 else default
    return default


def _normalize_agent_md_path(config_dict: Dict[str, Any], config_dir: str) -> None:
    agent_md_path = config_dict.get("agent_md_path")
    if (
        isinstance(agent_md_path, str)
        and agent_md_path.strip()
        and not os.path.isabs(agent_md_path)
    ):
        config_dict["agent_md_path"] = os.path.join(config_dir, agent_md_path)


def _normalize_chat_system_prompt_path(config_dict: Dict[str, Any], config_dir: str) -> None:
    chat_prompt_path = config_dict.get("chat_completions_system_prompt_path")
    if isinstance(chat_prompt_path, str) and chat_prompt_path.strip():
        config_dict["chat_completions_system_prompt_path"] = _resolve_path_from_config_dir(
            chat_prompt_path.strip(), config_dir
        )
    else:
        config_dict["chat_completions_system_prompt_path"] = os.path.join(
            config_dir, "prompts", "chat_completions_system_prompt.md"
        )


def _normalize_backend_api_mode(config_dict: Dict[str, Any]) -> None:
    backend_api_mode_raw = config_dict.get("backend_api_mode", "responses")
    backend_api_mode = str(backend_api_mode_raw).strip().lower()
    if backend_api_mode not in {"responses", "chat_completions"}:
        raise ValueError(
            "backend_api_mode должен быть 'responses' или 'chat_completions'"
        )
    config_dict["backend_api_mode"] = backend_api_mode


def _normalize_chat_completions_auth(config_dict: Dict[str, Any]) -> None:
    auth_token_raw = config_dict.get("chat_completions_auth_token")
    openrouter_api_key_raw = config_dict.get("openrouter_api_key")
    # Для удобства поддерживаем openrouter_api_key как алиас универсального
    # chat_completions_auth_token.
    effective_token_raw = (
        auth_token_raw if auth_token_raw is not None else openrouter_api_key_raw
    )
    if isinstance(effective_token_raw, str) and effective_token_raw.strip():
        config_dict["chat_completions_auth_token"] = effective_token_raw.strip()
    else:
        config_dict["chat_completions_auth_token"] = None

    http_referer_raw = config_dict.get("openrouter_http_referer")
    if isinstance(http_referer_raw, str) and http_referer_raw.strip():
        config_dict["openrouter_http_referer"] = http_referer_raw.strip()
    else:
        config_dict["openrouter_http_referer"] = None

    x_title_raw = config_dict.get("openrouter_x_title")
    if isinstance(x_title_raw, str) and x_title_raw.strip():
        config_dict["openrouter_x_title"] = x_title_raw.strip()
    else:
        config_dict["openrouter_x_title"] = None


def _normalize_summary_params(config_dict: Dict[str, Any], config_dir: str) -> None:
    summary_params_raw = config_dict.get("summary_params")
    summary_params = summary_params_raw if isinstance(summary_params_raw, dict) else {}

    summary_prompt_path = summary_params.get("chat_completions_summary_prompt_path")
    if isinstance(summary_prompt_path, str) and summary_prompt_path.strip():
        normalized_summary_prompt_path = _resolve_path_from_config_dir(
            summary_prompt_path.strip(), config_dir
        )
    else:
        normalized_summary_prompt_path = os.path.join(
            config_dir,
            "prompts",
            "chat_completions_summary_prompt.md",
        )

    summary_trigger = _safe_int(
        summary_params.get("chat_completions_summary_trigger_chars", 0), 0
    )
    steps_count = _safe_int(
        summary_params.get(
            "last_dialog_steps_count", _DEFAULT_LAST_DIALOG_STEPS_COUNT
        ),
        _DEFAULT_LAST_DIALOG_STEPS_COUNT,
    )
    message_max_chars = _safe_int(
        summary_params.get(
            "last_dialog_step_message_max_chars",
            _DEFAULT_LAST_DIALOG_STEP_MESSAGE_MAX_CHARS,
        ),
        _DEFAULT_LAST_DIALOG_STEP_MESSAGE_MAX_CHARS,
    )
    max_message_chars = _safe_int(
        summary_params.get("max_message_chars", _DEFAULT_MAX_MESSAGE_CHARS),
        _DEFAULT_MAX_MESSAGE_CHARS,
    )
    max_dialogue_chars = _safe_int(
        summary_params.get("max_dialogue_chars", _DEFAULT_MAX_DIALOGUE_CHARS),
        _DEFAULT_MAX_DIALOGUE_CHARS,
    )
    max_accum_summary_chars = _safe_int(
        summary_params.get(
            "max_accumulated_summary_chars", _DEFAULT_MAX_ACCUMULATED_SUMMARY_CHARS
        ),
        _DEFAULT_MAX_ACCUMULATED_SUMMARY_CHARS,
    )
    original_user_messages_max_chars = _safe_int(
        summary_params.get(
            "original_user_messages_max_chars",
            _DEFAULT_ORIGINAL_USER_MESSAGES_MAX_CHARS,
        ),
        _DEFAULT_ORIGINAL_USER_MESSAGES_MAX_CHARS,
    )
    incremental_batch_chars_raw = summary_params.get("incremental_batch_chars")
    derived_incremental_default = int(
        summary_trigger * _DEFAULT_INCREMENTAL_BATCH_MULTIPLIER
    )
    incremental_batch_chars = _safe_int(
        incremental_batch_chars_raw, derived_incremental_default
    )

    if steps_count <= 0:
        steps_count = _DEFAULT_LAST_DIALOG_STEPS_COUNT
    if message_max_chars <= 0:
        message_max_chars = _DEFAULT_LAST_DIALOG_STEP_MESSAGE_MAX_CHARS
    if max_message_chars <= 0:
        max_message_chars = _DEFAULT_MAX_MESSAGE_CHARS
    if max_dialogue_chars <= 0:
        max_dialogue_chars = _DEFAULT_MAX_DIALOGUE_CHARS
    if max_accum_summary_chars <= 0:
        max_accum_summary_chars = _DEFAULT_MAX_ACCUMULATED_SUMMARY_CHARS
    if original_user_messages_max_chars <= 0:
        original_user_messages_max_chars = _DEFAULT_ORIGINAL_USER_MESSAGES_MAX_CHARS
    if incremental_batch_chars <= 0:
        incremental_batch_chars = (
            int(summary_trigger * _DEFAULT_INCREMENTAL_BATCH_MULTIPLIER)
            if summary_trigger > 0
            else 0
        )

    config_dict["summary_params"] = {
        "chat_completions_summary_prompt_path": normalized_summary_prompt_path,
        "chat_completions_summary_trigger_chars": summary_trigger,
        "incremental_batch_chars": incremental_batch_chars,
        "last_dialog_steps_count": steps_count,
        "last_dialog_step_message_max_chars": message_max_chars,
        "max_message_chars": max_message_chars,
        "max_dialogue_chars": max_dialogue_chars,
        "max_accumulated_summary_chars": max_accum_summary_chars,
        "original_user_messages_max_chars": original_user_messages_max_chars,
    }


def _apply_swarm_env_overrides(config_dict: Dict[str, Any], config_dir: str) -> None:
    _maybe_override_and_resolve(
        os.environ.get("SWARM_AGENTMD_PATH"), "agent_md_path", config_dict, config_dir
    )
    _maybe_override_and_resolve(
        os.environ.get("SWARM_THREADS_DIR"), "threads_data_path", config_dict, config_dir
    )
    _maybe_override_and_resolve(
        os.environ.get("SWARM_SKILLS_DIR"), "skills_data_path", config_dict, config_dir
    )


def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Загрузить конфигурацию из YAML-файла.

    - Если config_path не задан, используется путь по умолчанию:
      <project_root>/config.yml (как и раньше).
    - При use_ssl == False отключаются предупреждения urllib3.
    """
    path = config_path or _default_config_path()
    config_dir = os.path.dirname(os.path.abspath(path))

    with open(path, "r", encoding="utf-8") as file:
        config_dict = yaml.safe_load(file) or {}
    _normalize_agent_md_path(config_dict, config_dir)
    _normalize_chat_system_prompt_path(config_dict, config_dir)
    _normalize_summary_params(config_dict, config_dir)
    _apply_swarm_env_overrides(config_dict, config_dir)

    verify = config_dict.get("use_ssl", True)
    if verify is False:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    _normalize_backend_api_mode(config_dict)
    _normalize_chat_completions_auth(config_dict)

    return config_dict


def build_auth_connector(config_dict: Dict[str, Any]) -> Optional[AuthConnector]:
    """
    Построить AuthConnector на основе секции agent_auth из config_dict.

    Если секция agent_auth отсутствует, возвращает None.
    """
    verify = config_dict.get("use_ssl", True)
    auth_cfg = config_dict.get("agent_auth") or {}
    if not auth_cfg:
        return None

    entry_point = auth_cfg["auth_endpoint"]
    env_name = auth_cfg.get("env_name", "surface_cli")
    env_version = auth_cfg.get("env_version", "0.0.1")
    plugin_name = auth_cfg.get("plugin_name", "surf-cli-plugin")
    plugin_version = auth_cfg.get("plugin_version", "0.0.1")
    device_code = auth_cfg.get("device_code", str(uuid4()))
    device_code_hex = auth_cfg.get("device_code_hex")
    login = auth_cfg.get("login", "login")

    return AuthConnector(
        entry_point=entry_point,
        env_name=env_name,
        env_version=env_version,
        plugin_name=plugin_name,
        plugin_version=plugin_version,
        device_code=device_code,
        device_code_hex=device_code_hex,
        login=login,
        verify=verify,
    )


if __name__ == "__main__":
    cfg = load_config()
    print(cfg)
    auth = build_auth_connector(cfg)
    if auth:
        print(auth.get_token())
