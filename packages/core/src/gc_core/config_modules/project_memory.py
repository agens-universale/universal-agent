from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict

from gc_core.config_modules.editing import editable

_logger = logging.getLogger(__name__)

DEFAULT_PROJECT_MEMORY_MAX_CHARS = 10_000
DEFAULT_PROJECT_MEMORY_MAX_APPEND_CHARS = 3_000
DEFAULT_PROJECT_MEMORY_CHECK_INTERVAL_MESSAGES = 5
# Потолок ответа compaction (не квота). Запас под мысль модели при TUI-дефолте
# без anti-thinking kwargs; фактический сжатый текст обычно сильно короче.
DEFAULT_PROJECT_MEMORY_COMPACTION_MAX_TOKENS = 16_384


class ProjectMemoryConfig(BaseModel):
    """Yaml section ``project_memory:`` — limits and notice policy for long memory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enable_project_memory_notice: bool = False
    check_interval_messages: int = (
        DEFAULT_PROJECT_MEMORY_CHECK_INTERVAL_MESSAGES
    )
    max_chars: int = DEFAULT_PROJECT_MEMORY_MAX_CHARS
    max_append_chars: int = DEFAULT_PROJECT_MEMORY_MAX_APPEND_CHARS
    compaction_max_tokens: int = DEFAULT_PROJECT_MEMORY_COMPACTION_MAX_TOKENS


def normalize_project_memory_config(
    cfg: ProjectMemoryConfig,
) -> ProjectMemoryConfig:
    cfg = editable(cfg)
    if cfg.check_interval_messages <= 0:
        _logger.warning(
            "project_memory.check_interval_messages=%s must be > 0; using default %s",
            cfg.check_interval_messages,
            DEFAULT_PROJECT_MEMORY_CHECK_INTERVAL_MESSAGES,
        )
        cfg.check_interval_messages = (
            DEFAULT_PROJECT_MEMORY_CHECK_INTERVAL_MESSAGES
        )
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
    if cfg.compaction_max_tokens <= 0:
        _logger.warning(
            "project_memory.compaction_max_tokens=%s must be > 0; using default %s",
            cfg.compaction_max_tokens,
            DEFAULT_PROJECT_MEMORY_COMPACTION_MAX_TOKENS,
        )
        cfg.compaction_max_tokens = (
            DEFAULT_PROJECT_MEMORY_COMPACTION_MAX_TOKENS
        )
    return cfg.result()


def project_memory_from_config_dict(
    config_dict: Any,
) -> ProjectMemoryConfig:
    """``ProjectMemoryConfig`` настроек (``config_dict`` — всё, что принимает ``settings_of``)."""
    # Ленивый импорт: settings → root → этот модуль.
    from gc_core.settings import settings_of

    return settings_of(config_dict).project_memory


def project_memory_limits_from_config_dict(
    config_dict: Any,
) -> dict[str, int]:
    cfg = project_memory_from_config_dict(config_dict)
    return {
        "max_chars": cfg.max_chars,
        "max_append_chars": cfg.max_append_chars,
    }


def project_memory_notice_enabled(config_dict: Any) -> bool:
    """Project memory exists/changed hints: только явный флаг в ``project_memory``."""
    return project_memory_from_config_dict(
        config_dict
    ).enable_project_memory_notice
