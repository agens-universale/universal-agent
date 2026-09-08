"""Error categories and domain exception for computer-mcp."""

from enum import Enum


class ErrorCategory(str, Enum):
    ELEMENT_NOT_FOUND = "element_not_found"
    STALE_STATE = "stale_state"
    READONLY_VIOLATION = "readonly_violation"
    BLACKLISTED_KEYS = "blacklisted_keys"
    OUT_OF_BOUNDS = "out_of_bounds"
    TIMEOUT = "timeout"
    UIA_UNAVAILABLE = "uia_unavailable"
    APP_NOT_FOUND = "app_not_found"
    INVALID_INPUT = "invalid_input"
    DPI_ERROR = "dpi_error"
    UNEXPECTED = "unexpected"


class ComputerMCPError(Exception):
    """Single domain exception carrying a machine-readable category."""

    def __init__(self, category: ErrorCategory, message: str) -> None:
        self.category = category
        super().__init__(f"[{category.value}] {message}")
