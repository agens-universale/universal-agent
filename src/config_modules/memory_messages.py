from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict

_logger = logging.getLogger(__name__)

DEFAULT_MEMORY_GATE_COOLDOWN_MESSAGES = 30
DEFAULT_LONG_MEMORY_CHECK_INTERVAL_MESSAGES = 5


class MemoryGateConfig(BaseModel):
    """Порог wiki gate: technical «not used memory_files» раз в N persist-шагов."""

    model_config = ConfigDict(extra="forbid")

    enable_memory_file_notice: bool = False
    cooldown_messages: int = DEFAULT_MEMORY_GATE_COOLDOWN_MESSAGES


class LongMemoryMessagesConfig(BaseModel):
    """Интервал проверки hash long memory (K persist-шагов); счётчик в meta, не в State."""

    model_config = ConfigDict(extra="forbid")

    enable_long_memory_notice: bool = False
    check_interval_messages: int = DEFAULT_LONG_MEMORY_CHECK_INTERVAL_MESSAGES


def normalize_memory_gate_config(cfg: MemoryGateConfig) -> None:
    if cfg.cooldown_messages <= 0:
        _logger.warning(
            "memory_gate.cooldown_messages=%s must be > 0; using default %s",
            cfg.cooldown_messages,
            DEFAULT_MEMORY_GATE_COOLDOWN_MESSAGES,
        )
        cfg.cooldown_messages = DEFAULT_MEMORY_GATE_COOLDOWN_MESSAGES


def normalize_long_memory_messages_config(cfg: LongMemoryMessagesConfig) -> None:
    if cfg.check_interval_messages <= 0:
        _logger.warning(
            "long_memory.check_interval_messages=%s must be > 0; using default %s",
            cfg.check_interval_messages,
            DEFAULT_LONG_MEMORY_CHECK_INTERVAL_MESSAGES,
        )
        cfg.check_interval_messages = DEFAULT_LONG_MEMORY_CHECK_INTERVAL_MESSAGES


def memory_gate_from_config_dict(config_dict: dict[str, Any] | None) -> MemoryGateConfig:
    raw = (config_dict or {}).get("memory_gate")
    if isinstance(raw, MemoryGateConfig):
        return raw
    params = (config_dict or {}).get("memory_gate_params")
    if isinstance(params, dict):
        return MemoryGateConfig.model_validate(params)
    return MemoryGateConfig()


def long_memory_messages_from_config_dict(
    config_dict: dict[str, Any] | None,
) -> LongMemoryMessagesConfig:
    raw = (config_dict or {}).get("long_memory")
    if isinstance(raw, LongMemoryMessagesConfig):
        return raw
    params = (config_dict or {}).get("long_memory_params")
    if isinstance(params, dict):
        return LongMemoryMessagesConfig.model_validate(params)
    return LongMemoryMessagesConfig()


def memory_file_notice_enabled(config_dict: dict[str, Any] | None) -> bool:
    """Wiki gate / subagent bootstrap: только явный флаг в ``memory_gate``."""
    if (config_dict or {}).get("enable_memory_file_notice") is True:
        return True
    return memory_gate_from_config_dict(config_dict).enable_memory_file_notice


def long_memory_notice_enabled(config_dict: dict[str, Any] | None) -> bool:
    """Long exists/changed hints: только явный флаг в ``long_memory``."""
    if (config_dict or {}).get("enable_long_memory_notice") is True:
        return True
    return long_memory_messages_from_config_dict(config_dict).enable_long_memory_notice
