from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict

_logger = logging.getLogger(__name__)

DEFAULT_MAX_FILE_CHARS = 20_000
DEFAULT_TOTAL_BUDGET_CHARS = 50_000
DEFAULT_MAX_FILES = 5


class MemoryFilesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_file_chars: int = DEFAULT_MAX_FILE_CHARS
    total_budget_chars: int = DEFAULT_TOTAL_BUDGET_CHARS
    max_files: int = DEFAULT_MAX_FILES


def normalize_memory_files_config(cfg: MemoryFilesConfig) -> None:
    if cfg.max_file_chars <= 0:
        _logger.warning(
            "memory_files.max_file_chars=%s must be > 0; using default %s",
            cfg.max_file_chars,
            DEFAULT_MAX_FILE_CHARS,
        )
        cfg.max_file_chars = DEFAULT_MAX_FILE_CHARS
    if cfg.total_budget_chars <= 0:
        _logger.warning(
            "memory_files.total_budget_chars=%s must be > 0; using default %s",
            cfg.total_budget_chars,
            DEFAULT_TOTAL_BUDGET_CHARS,
        )
        cfg.total_budget_chars = DEFAULT_TOTAL_BUDGET_CHARS
    if cfg.max_files <= 0:
        _logger.warning(
            "memory_files.max_files=%s must be > 0; using default %s",
            cfg.max_files,
            DEFAULT_MAX_FILES,
        )
        cfg.max_files = DEFAULT_MAX_FILES


def memory_files_limits_from_params(params: dict | None) -> dict[str, int]:
    if not isinstance(params, dict):
        params = {}
    return {
        "max_file_chars": int(params.get("max_file_chars", DEFAULT_MAX_FILE_CHARS)),
        "total_budget_chars": int(
            params.get("total_budget_chars", DEFAULT_TOTAL_BUDGET_CHARS)
        ),
        "max_files": int(params.get("max_files", DEFAULT_MAX_FILES)),
    }
