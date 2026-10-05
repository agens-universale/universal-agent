from __future__ import annotations

from typing import Any, Dict, Optional
from uuid import uuid4

from gc_core.utils.prod_auth import AuthConnector


def build_auth_connector(
    settings: Any,
) -> Optional[AuthConnector]:
    """AuthConnector по секции ``agent_auth`` (None — секция пуста или не задана).

    ``settings`` — всё, что принимает ``settings_of``.
    """
    # Ленивый импорт: settings → root → config_modules.
    from gc_core.settings import settings_of

    settings = settings_of(settings)
    verify = settings.backend.use_ssl
    auth_cfg: Dict[str, Any] = (
        settings.agent_auth.model_dump() if settings.agent_auth else {}
    )
    if not auth_cfg:
        return None

    entry_point = auth_cfg.get("auth_endpoint")
    if not entry_point:
        raise ValueError(
            "agent_auth.auth_endpoint is required when agent_auth is configured"
        )
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
        config_proxy=settings.backend.proxy,
    )
