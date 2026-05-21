from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_logger = logging.getLogger(__name__)

DEFAULT_INTENT_SCOPE_MAX_SUMMARY_CHARS = 500
DEFAULT_INTENT_SCOPE_MAX_TOOL_OUTPUT_CHARS = 8000


class IntentScopeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tools: list[str] = Field(default_factory=list)
    max_summary_chars: int = DEFAULT_INTENT_SCOPE_MAX_SUMMARY_CHARS
    max_tool_output_chars: int = DEFAULT_INTENT_SCOPE_MAX_TOOL_OUTPUT_CHARS
    fallback_raw_on_llm_error: bool = False
    intent_scope_tool_summary_prompt_path: str | None = None


def normalize_intent_scope_config(cfg: IntentScopeConfig) -> None:
    if cfg.max_summary_chars <= 0:
        _logger.warning(
            "intent_scope.max_summary_chars=%s must be > 0; using %s",
            cfg.max_summary_chars,
            DEFAULT_INTENT_SCOPE_MAX_SUMMARY_CHARS,
        )
        cfg.max_summary_chars = DEFAULT_INTENT_SCOPE_MAX_SUMMARY_CHARS
    if cfg.max_tool_output_chars <= 0:
        _logger.warning(
            "intent_scope.max_tool_output_chars=%s must be > 0; using %s",
            cfg.max_tool_output_chars,
            DEFAULT_INTENT_SCOPE_MAX_TOOL_OUTPUT_CHARS,
        )
        cfg.max_tool_output_chars = DEFAULT_INTENT_SCOPE_MAX_TOOL_OUTPUT_CHARS


def intent_scope_active(cfg: IntentScopeConfig) -> bool:
    return bool(cfg.tools)


def intent_scope_from_config_dict(config_dict: dict[str, Any] | None) -> IntentScopeConfig:
    """Собрать IntentScopeConfig из ``config_dict`` (после ``load_config``)."""
    raw = (config_dict or {}).get("intent_scope")
    if isinstance(raw, IntentScopeConfig):
        return raw
    params = (config_dict or {}).get("intent_scope_params")
    if isinstance(params, dict):
        return IntentScopeConfig.model_validate(params)
    return IntentScopeConfig()


def backend_allows_intent_scope_hook(config_dict: dict[str, Any] | None) -> bool:
    raw_mode = str((config_dict or {}).get("backend_api_mode", "responses")).strip().lower()
    return raw_mode == "chat_completions"
