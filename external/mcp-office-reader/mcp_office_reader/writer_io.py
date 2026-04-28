from __future__ import annotations

import base64
import os

from .errors import ErrorCategory, OfficeReaderError
from .security import validate_path


def write_bytes_checked(
    path: str,
    data: bytes,
    *,
    output_allowed_dirs: list[str],
    allow_any: bool,
    overwrite: bool,
) -> int:
    try:
        real = validate_path(path, output_allowed_dirs, allow_any=allow_any)
    except OfficeReaderError:
        raise  # already typed
    real_str = str(real)
    if os.path.exists(real_str) and not overwrite:
        raise OfficeReaderError(ErrorCategory.FILE_EXISTS, path=real_str)
    try:
        parent = os.path.dirname(real_str)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(real_str, "wb") as f:
            f.write(data)
    except PermissionError as e:
        raise OfficeReaderError(
            ErrorCategory.WRITE_FAILED, path=real_str, reason=str(e)
        ) from e
    except OSError as e:
        raise OfficeReaderError(
            ErrorCategory.WRITE_FAILED, path=real_str, reason=str(e)
        ) from e
    return len(data)


def encode_base64_checked(data: bytes, *, max_base64_bytes: int) -> dict:
    encoded = base64.b64encode(data).decode("ascii")
    if len(encoded) > max_base64_bytes:
        raise OfficeReaderError(
            ErrorCategory.OUTPUT_TOO_LARGE,
            size_mb=round(len(encoded) / 1024 / 1024, 2),
            max_mb=round(max_base64_bytes / 1024 / 1024, 2),
        )
    return {"content_base64": encoded, "raw_bytes": len(data)}
