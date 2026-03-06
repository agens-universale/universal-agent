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

    with open(path, "r", encoding="utf-8") as file:
        config_dict = yaml.safe_load(file) or {}

    verify = config_dict.get("use_ssl", True)
    if verify is False:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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
