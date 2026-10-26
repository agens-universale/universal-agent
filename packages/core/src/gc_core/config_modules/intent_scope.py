from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gc_core.config_modules.editing import editable
from gc_core.config_modules.field_meta import gc_meta

_logger = logging.getLogger(__name__)

DEFAULT_INTENT_SCOPE_MAX_SUMMARY_CHARS = 500
DEFAULT_INTENT_SCOPE_MAX_TOOL_OUTPUT_CHARS = 8000
DEFAULT_INTENT_SCOPE_MAX_RAW_RECORD_CHARS = 5000
DEFAULT_INTENT_SCOPE_MAX_FINAL_ANSWER_RAW_CHARS = 2000
DEFAULT_INTENT_LOG_READ_PAGE_RECORD_LIMIT = 10

IntentScopeRecordMode = Literal["aggregated", "raw"]


class IntentScopeToolConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record: IntentScopeRecordMode = "aggregated"
    store_arguments: bool = True
    store_output: bool = True

    @model_validator(mode="after")
    def _validate_store_flags(self) -> IntentScopeToolConfig:
        if (
            self.record == "raw"
            and not self.store_arguments
            and not self.store_output
        ):
            raise ValueError(
                "intent_scope.tools.* raw config requires at least one of store_arguments/store_output to be true"
            )
        return self


class IntentScopeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tools: dict[str, IntentScopeToolConfig] = Field(
        default_factory=dict, json_schema_extra=gc_meta(expert=True)
    )
    max_summary_chars: int = Field(
        default=DEFAULT_INTENT_SCOPE_MAX_SUMMARY_CHARS,
        json_schema_extra=gc_meta(expert=True),
    )
    max_tool_output_chars: int = Field(
        default=DEFAULT_INTENT_SCOPE_MAX_TOOL_OUTPUT_CHARS,
        json_schema_extra=gc_meta(expert=True),
    )
    max_raw_record_chars: int = Field(
        default=DEFAULT_INTENT_SCOPE_MAX_RAW_RECORD_CHARS,
        json_schema_extra=gc_meta(expert=True),
    )
    max_final_answer_raw_chars: int = (
        DEFAULT_INTENT_SCOPE_MAX_FINAL_ANSWER_RAW_CHARS
    )
    read_page_record_limit: int = Field(
        default=DEFAULT_INTENT_LOG_READ_PAGE_RECORD_LIMIT,
        json_schema_extra=gc_meta(expert=True),
    )
    fallback_raw_on_llm_error: bool = Field(
        default=True, json_schema_extra=gc_meta(expert=True)
    )
    intent_scope_tool_summary_prompt_path: str | None = Field(
        default=None, json_schema_extra=gc_meta(expert=True)
    )
    intent_scope_final_answer_summary_prompt_path: str | None = Field(
        default=None, json_schema_extra=gc_meta(expert=True)
    )


def normalize_intent_scope_config(cfg: IntentScopeConfig) -> IntentScopeConfig:
    cfg = editable(cfg)
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
    if cfg.max_raw_record_chars <= 0:
        _logger.warning(
            "intent_scope.max_raw_record_chars=%s must be > 0; using %s",
            cfg.max_raw_record_chars,
            DEFAULT_INTENT_SCOPE_MAX_RAW_RECORD_CHARS,
        )
        cfg.max_raw_record_chars = DEFAULT_INTENT_SCOPE_MAX_RAW_RECORD_CHARS
    if cfg.max_final_answer_raw_chars <= 0:
        _logger.warning(
            "intent_scope.max_final_answer_raw_chars=%s must be > 0; using %s",
            cfg.max_final_answer_raw_chars,
            DEFAULT_INTENT_SCOPE_MAX_FINAL_ANSWER_RAW_CHARS,
        )
        cfg.max_final_answer_raw_chars = (
            DEFAULT_INTENT_SCOPE_MAX_FINAL_ANSWER_RAW_CHARS
        )
    if cfg.read_page_record_limit <= 0:
        _logger.warning(
            "intent_scope.read_page_record_limit=%s must be > 0; using %s",
            cfg.read_page_record_limit,
            DEFAULT_INTENT_LOG_READ_PAGE_RECORD_LIMIT,
        )
        cfg.read_page_record_limit = DEFAULT_INTENT_LOG_READ_PAGE_RECORD_LIMIT
    return cfg.result()


def intent_scope_active(cfg: IntentScopeConfig) -> bool:
    return bool(cfg.tools)


def intent_scope_tool_record(
    cfg: IntentScopeConfig, tool_name: str
) -> IntentScopeRecordMode | None:
    tool_cfg = cfg.tools.get(tool_name)
    if tool_cfg is None:
        return None
    return tool_cfg.record


def intent_scope_tool_store_flags(
    cfg: IntentScopeConfig, tool_name: str
) -> tuple[bool, bool]:
    tool_cfg = cfg.tools.get(tool_name)
    if tool_cfg is None:
        return True, True
    return bool(tool_cfg.store_arguments), bool(tool_cfg.store_output)


def intent_scope_from_config_dict(
    settings: Any,
) -> IntentScopeConfig:
    """``IntentScopeConfig`` настроек (``settings`` — всё, что принимает ``settings_of``)."""
    # Ленивый импорт: settings → root → этот модуль.
    from gc_core.settings import settings_of

    return settings_of(settings).intent_scope
