import os
from typing import Optional, Dict, Any
from uuid import uuid4

import urllib3
import yaml

from utils.prod_auth import AuthConnector


def _default_config_path() -> str:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, "..", "config.yml")


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

    def _resolve_path_from_config_dir(path_value: str) -> str:
        if os.path.isabs(path_value):
            return path_value
        return os.path.join(config_dir, path_value)

    def _maybe_override_and_resolve(maybe_env_value: Optional[str], key: str) -> None:
        """
        Override config_dict[key] from env and keep it platform independent.
        If env value is relative - resolve it relative to config.yml directory.
        """
        if not isinstance(maybe_env_value, str):
            return
        env_value = maybe_env_value.strip()
        if not env_value:
            return
        config_dict[key] = _resolve_path_from_config_dir(env_value)

    # Приводим относительные пути к абсолютным относительно каталога config.yml.
    # Это важно для корректной подстановки promptVariables.userInstructions,
    # т.к. в State используется open(agent_md_path) без дополнительного cwd-резолва.
    agent_md_path = config_dict.get("agent_md_path")
    if isinstance(agent_md_path, str) and agent_md_path.strip() and not os.path.isabs(agent_md_path):
        config_dict["agent_md_path"] = os.path.join(config_dir, agent_md_path)

    # Путь к локальному системному промпту для chat/completions.
    # Если не задан в config, используем файл в корне проекта.
    chat_prompt_path = config_dict.get("chat_completions_system_prompt_path")
    if isinstance(chat_prompt_path, str) and chat_prompt_path.strip():
        config_dict["chat_completions_system_prompt_path"] = _resolve_path_from_config_dir(
            chat_prompt_path.strip()
        )
    else:
        config_dict["chat_completions_system_prompt_path"] = os.path.join(
            config_dir, "chat_completions_system_prompt.md"
        )

    # Swarm-режим прокидывает путь до agent.md / threads / skills через SWARM_* env.
    # Раньше эти env никак не влияли на CLI-запуск в песочнице, из-за чего
    # swarm-ручка могла использовать не тот agent.md.
    _maybe_override_and_resolve(os.environ.get("SWARM_AGENTMD_PATH"), "agent_md_path")
    _maybe_override_and_resolve(os.environ.get("SWARM_THREADS_DIR"), "threads_data_path")
    _maybe_override_and_resolve(os.environ.get("SWARM_SKILLS_DIR"), "skills_data_path")

    verify = config_dict.get("use_ssl", True)
    if verify is False:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    backend_api_mode_raw = config_dict.get("backend_api_mode", "responses")
    backend_api_mode = str(backend_api_mode_raw).strip().lower()
    if backend_api_mode not in {"responses", "chat_completions"}:
        raise ValueError(
            "backend_api_mode должен быть 'responses' или 'chat_completions'"
        )
    config_dict["backend_api_mode"] = backend_api_mode

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
