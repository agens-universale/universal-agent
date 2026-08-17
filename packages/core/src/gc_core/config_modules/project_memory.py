from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict

_logger = logging.getLogger(__name__)

DEFAULT_PROJECT_MEMORY_MAX_CHARS = 10_000
DEFAULT_PROJECT_MEMORY_MAX_APPEND_CHARS = 3_000
DEFAULT_PROJECT_MEMORY_CHECK_INTERVAL_MESSAGES = 5


class ProjectMemoryConfig(BaseModel):
    """Yaml section ``project_memory:`` — limits and notice policy for long memory."""

    model_config = ConfigDict(extra="forbid")

    enable_project_memory_notice: bool = False
    check_interval_messages: int = DEFAULT_PROJECT_MEMORY_CHECK_INTERVAL_MESSAGES
    max_chars: int = DEFAULT_PROJECT_MEMORY_MAX_CHARS
    max_append_chars: int = DEFAULT_PROJECT_MEMORY_MAX_APPEND_CHARS


def normalize_project_memory_config(cfg: ProjectMemoryConfig) -> None:
    if cfg.check_interval_messages <= 0:
        _logger.warning(
            "project_memory.check_interval_messages=%s must be > 0; using default %s",
            cfg.check_interval_messages,
            DEFAULT_PROJECT_MEMORY_CHECK_INTERVAL_MESSAGES,
        )
        cfg.check_interval_messages = DEFAULT_PROJECT_MEMORY_CHECK_INTERVAL_MESSAGES
    if cfg.max_chars <= 0:
        _logger.warning(
            "project_memory.max_chars=%s must be > 0; using default %s",
            cfg.max_chars,
            DEFAULT_PROJECT_MEMORY_MAX_CHARS,
        )
        cfg.max_chars = DEFAULT_PROJECT_MEMORY_MAX_CHARS
    if cfg.max_append_chars <= 0:
        _logger.warning(
            "project_memory.max_append_chars=%s must be > 0; using default %s",
            cfg.max_append_chars,
            DEFAULT_PROJECT_MEMORY_MAX_APPEND_CHARS,
        )
        cfg.max_append_chars = DEFAULT_PROJECT_MEMORY_MAX_APPEND_CHARS


def project_memory_from_config_dict(
    config_dict: dict[str, Any] | None,
) -> ProjectMemoryConfig:
    raw = (config_dict or {}).get("project_memory")
    if isinstance(raw, ProjectMemoryConfig):
        return raw
    params = (config_dict or {}).get("project_memory_params")
    if isinstance(params, dict):
        return ProjectMemoryConfig.model_validate(params)
    return ProjectMemoryConfig()


def project_memory_limits_from_config_dict(
    config_dict: dict[str, Any] | None,
) -> dict[str, int]:
    cfg = project_memory_from_config_dict(config_dict)
    return {
        "max_chars": cfg.max_chars,
        "max_append_chars": cfg.max_append_chars,
    }


def project_memory_notice_enabled(config_dict: dict[str, Any] | None) -> bool:
    """Project memory exists/changed hints: только явный флаг в ``project_memory``."""
    if (config_dict or {}).get("enable_project_memory_notice") is True:
        return True
    return project_memory_from_config_dict(config_dict).enable_project_memory_notice
