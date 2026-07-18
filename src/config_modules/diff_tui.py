"""Конфиг секции ``diff_tui:`` — лимиты cache и visual diff в TUI."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


DEFAULT_CACHE_MAX_ORIG = 500
DEFAULT_PREVIEW_CONTEXT_LINES = 3
DEFAULT_MAX_DIFF_CHARS = 100_000


class DiffTuiConfig(BaseModel):
    """Yaml section ``diff_tui:`` — visual Diff-TUI, не параметры fs-тулов."""

    model_config = ConfigDict(extra="forbid")

    cache_max_orig: int = DEFAULT_CACHE_MAX_ORIG
    preview_context_lines: int = DEFAULT_PREVIEW_CONTEXT_LINES
    max_diff_chars: int = DEFAULT_MAX_DIFF_CHARS
