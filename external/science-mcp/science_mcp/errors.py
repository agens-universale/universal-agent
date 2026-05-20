from __future__ import annotations

from enum import Enum
from typing import Any, Optional


class ErrorCategory(str, Enum):
    RATE_LIMITED = "rate_limited"
    NOT_FOUND = "not_found"
    INVALID_ARGUMENT = "invalid_argument"
    UPSTREAM_ERROR = "upstream_error"
    TIMEOUT = "timeout"
    UNAUTHORIZED = "unauthorized"
    RESPONSE_TOO_LARGE = "response_too_large"


def format_error(category: ErrorCategory, **ctx: Any) -> str:
    match category:
        case ErrorCategory.RATE_LIMITED:
            src = ctx.get("source", "upstream")
            return f"Error: rate limit hit for {src}. Retry after backoff."
        case ErrorCategory.NOT_FOUND:
            ident = ctx.get("identifier", "(unknown)")
            return f"Error: not found: {ident}."
        case ErrorCategory.INVALID_ARGUMENT:
            reason = ctx.get("reason", "(no detail)")
            return f"Error: invalid argument: {reason}."
        case ErrorCategory.UPSTREAM_ERROR:
            src = ctx.get("source", "upstream")
            detail = ctx.get("detail", "")
            return f"Error: upstream error from {src}: {detail}"
        case ErrorCategory.TIMEOUT:
            src = ctx.get("source", "upstream")
            return f"Error: timeout calling {src}."
        case ErrorCategory.UNAUTHORIZED:
            src = ctx.get("source", "upstream")
            return f"Error: unauthorized — missing API key for {src}."
        case ErrorCategory.RESPONSE_TOO_LARGE:
            size = ctx.get("size_bytes", 0)
            cap = ctx.get("max_bytes", 0)
            return (
                f"Error: response too large ({size} bytes, max {cap}). "
                f"Use a lighter mode (e.g. summary) or reduce ids."
            )


class ScienceMCPError(Exception):
    """Base. Maps to ToolError on the boundary."""

    def __init__(self, category: ErrorCategory, **context: Any) -> None:
        self.category = category
        self.context = context
        super().__init__(format_error(category, **context))

    @property
    def source(self) -> Optional[str]:
        return self.context.get("source")

    @property
    def retry_after_sec(self) -> Optional[int]:
        return self.context.get("retry_after_sec")
