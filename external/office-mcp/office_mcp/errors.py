from __future__ import annotations

from enum import Enum
from typing import Any

SUPPORTED_FORMATS_LIST = ".pdf, .docx, .xls, .xlsx, .pptx, .csv, .html, .msg"
LEGACY_HINT_EXTS = frozenset({".doc", ".ppt", ".odt", ".ods", ".odp", ".rtf"})


class ErrorCategory(str, Enum):
    FILE_NOT_FOUND = "file_not_found"
    PATH_NOT_ALLOWED = "path_not_allowed"
    UNSUPPORTED_FORMAT = "unsupported_format"
    FILE_TOO_LARGE = "file_too_large"
    PARSE_FAILED = "parse_failed"
    TIMEOUT = "timeout"
    INVALID_BASE64 = "invalid_base64"
    PERMISSION_DENIED = "permission_denied"
    INVALID_INPUT = "invalid_input"
    FILE_EXISTS = "file_exists"
    OUTPUT_TOO_LARGE = "output_too_large"
    WRITE_FAILED = "write_failed"
    COLUMN_NOT_FOUND = "column_not_found"
    UNSUPPORTED_AGGREGATION = "unsupported_aggregation"
    INCOMPATIBLE_DTYPE = "incompatible_dtype"


class OfficeReaderError(Exception):
    """Base exception carrying an ErrorCategory and render context."""

    def __init__(self, category: ErrorCategory, **context: Any) -> None:
        self.category = category
        self.context = context
        super().__init__(format_error(category, **context))


def format_error(category: ErrorCategory, **ctx: Any) -> str:
    match category:
        case ErrorCategory.FILE_NOT_FOUND:
            return f"Error: file not found: {ctx['path']}. Check that the path is correct."
        case ErrorCategory.PATH_NOT_ALLOWED:
            dirs = ", ".join(ctx["allowed_dirs"])
            return (
                f"Error: path not allowed: {ctx['path']}. "
                f"Server reads only from: {dirs}."
            )
        case ErrorCategory.UNSUPPORTED_FORMAT:
            base = (
                f"Error: unsupported format '{ctx['ext']}'. "
                f"Supported: {SUPPORTED_FORMATS_LIST}."
            )
            if ctx["ext"].lower() in LEGACY_HINT_EXTS:
                return (
                    base
                    + " Convert to a modern format first "
                    "(e.g. .docx / .xlsx / .pptx)."
                )
            return base
        case ErrorCategory.FILE_TOO_LARGE:
            return (
                f"Error: file too large: {ctx['size_mb']}MB "
                f"(max {ctx['max_mb']}MB)."
            )
        case ErrorCategory.PARSE_FAILED:
            return (
                f"Error: failed to parse {ctx['path']} as {ctx['fmt']}. "
                f"File may be corrupted or password-protected."
            )
        case ErrorCategory.TIMEOUT:
            return (
                f"Error: processing timed out after {ctx['timeout_s']}s. "
                f"File may be too complex."
            )
        case ErrorCategory.INVALID_BASE64:
            return "Error: content_base64 is not valid base64."
        case ErrorCategory.PERMISSION_DENIED:
            return f"Error: permission denied reading {ctx['path']}."
        case ErrorCategory.INVALID_INPUT:
            return f"Error: invalid input: {ctx['reason']}."
        case ErrorCategory.FILE_EXISTS:
            return (
                f"Error: file already exists: {ctx['path']}. "
                f"Pass overwrite=true to replace it."
            )
        case ErrorCategory.OUTPUT_TOO_LARGE:
            unit = ctx.get("unit", "MB")
            return (
                f"Error: result is {ctx['size_mb']}{unit} "
                f"(max {ctx['max_mb']}{unit}). "
                f"Use the *_file variant or output={{path: ...}} to write to disk."
            )
        case ErrorCategory.WRITE_FAILED:
            return (
                f"Error: failed to write {ctx['path']}: {ctx['reason']}."
            )
        case ErrorCategory.COLUMN_NOT_FOUND:
            available = ", ".join(ctx["available"])
            return (
                f"Error: column not found: {ctx['column']!r}. "
                f"Available columns: {available}."
            )
        case ErrorCategory.UNSUPPORTED_AGGREGATION:
            supported = ", ".join(ctx["supported"])
            return (
                f"Error: unsupported aggregation: {ctx['func']!r}. "
                f"Supported: {supported}."
            )
        case ErrorCategory.INCOMPATIBLE_DTYPE:
            return (
                f"Error: column {ctx['column']!r} has dtype "
                f"{ctx['dtype']!r}, which is incompatible with "
                f"operation {ctx['operation']!r}."
            )
