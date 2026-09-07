from typing import Any, Dict, Optional
from uuid import uuid4

from gc_core.utils.prod_auth import AuthConnector


def build_auth_connector(config_dict: Dict[str, Any]) -> Optional[AuthConnector]:
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
        config_proxy=config_dict.get("proxy"),
    )
