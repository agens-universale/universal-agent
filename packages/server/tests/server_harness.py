"""Поверхности webhook и swarm в тестах: без config.yml и config_server.yml хоста."""

from typing import Any

from gc_server.app import ServerRuntime
from gc_server.config import ServerConfig
from gc_testkit.settings import make_settings


def runtime(
    tmp_path=None, *, config: dict | None = None, **settings: Any
) -> ServerRuntime:
    """Рантайм процесса: настройки агента — умолчания (треды — в
    ``tmp_path/threads``), конфиг сервера — ``config``."""
    overrides = dict(settings)
    if tmp_path is not None:
        overrides.setdefault(
            "paths.threads_data_path", str(tmp_path / "threads")
        )
    return ServerRuntime(
        base=make_settings(overrides),
        config=ServerConfig.model_validate(config or {}),
    )


def make_webhook(tmp_path=None, *, config: dict | None = None, **settings):
    from gc_server.webhook.surface import WebhookSurface

    return WebhookSurface(runtime(tmp_path, config=config, **settings))


def make_swarm(swarm: dict, tmp_path=None, *, config: dict | None = None):
    from gc_server.swarm.surface import SwarmSurface

    return SwarmSurface(runtime(tmp_path, config=config), swarm=swarm)
