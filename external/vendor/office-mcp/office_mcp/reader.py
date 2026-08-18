from __future__ import annotations

import base64 as _b64
import binascii
import tempfile
from pathlib import Path

from markitdown import MarkItDown

from .errors import ErrorCategory, OfficeReaderError
from .xlsx_read import xlsx_to_markdown

SUPPORTED_EXTENSIONS: tuple[str, ...] = (
    ".pdf", ".docx", ".xls", ".xlsx", ".pptx", ".csv", ".html", ".msg",
)

_md = MarkItDown()


def _convert_with_markitdown(path: Path) -> str:
    if path.suffix.lower() == ".xlsx":
        return xlsx_to_markdown(path)
    result = _md.convert(str(path))
    return result.text_content


def read_file_from_disk(
    path: Path,
    max_file_size: int,
    max_output_chars: int,
    timeout_s: int,
) -> str:
    if not path.exists() or not path.is_file():
        raise OfficeReaderError(ErrorCategory.FILE_NOT_FOUND, path=str(path))
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise OfficeReaderError(ErrorCategory.UNSUPPORTED_FORMAT, ext=ext)

    try:
        size = path.stat().st_size
    except PermissionError as e:
        raise OfficeReaderError(ErrorCategory.PERMISSION_DENIED, path=str(path)) from e
    if size > max_file_size:
        raise OfficeReaderError(
            ErrorCategory.FILE_TOO_LARGE,
            size_mb=round(size / (1024 * 1024), 2),
            max_mb=round(max_file_size / (1024 * 1024), 2),
        )

    try:
        md = _convert_with_markitdown(path)
    except OfficeReaderError:
        raise
    except PermissionError as e:
        raise OfficeReaderError(ErrorCategory.PERMISSION_DENIED, path=str(path)) from e
    except Exception as e:
        raise OfficeReaderError(
            ErrorCategory.PARSE_FAILED, path=str(path), fmt=ext,
        ) from e

    total_chars = len(md)
    truncated_text, truncated = truncate_output(md, max_output_chars)
    returned = (
        f"1-{min(total_chars, max_output_chars)}" if truncated else f"1-{total_chars}"
    )
    meta = format_meta_block(
        fmt=ext.lstrip("."),
        total_chars=total_chars,
        returned_chars=returned,
    )
    return meta + "\n\n" + truncated_text


def read_document_from_bytes(
    filename: str,
    content_base64: str,
    max_file_size: int,
    max_output_chars: int,
    timeout_s: int,
) -> str:
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise OfficeReaderError(ErrorCategory.UNSUPPORTED_FORMAT, ext=ext)
    try:
        data = _b64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise OfficeReaderError(ErrorCategory.INVALID_BASE64) from e
    if len(data) > max_file_size:
        raise OfficeReaderError(
            ErrorCategory.FILE_TOO_LARGE,
            size_mb=round(len(data) / (1024 * 1024), 2),
            max_mb=round(max_file_size / (1024 * 1024), 2),
        )
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        return read_file_from_disk(
            tmp_path,
            max_file_size=max_file_size,
            max_output_chars=max_output_chars,
            timeout_s=timeout_s,
        )
    finally:
        tmp_path.unlink(missing_ok=True)


def format_meta_block(**fields: object) -> str:
    parts = [f"{'format' if k == 'fmt' else k}={v}" for k, v in fields.items()]
    return "[document: " + ", ".join(parts) + "]"


def truncate_output(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    truncated = text[:max_chars]
    total = len(text)
    marker = f"\n\n[truncated: returned {max_chars} of {total} characters.]"
    return truncated + marker, True
