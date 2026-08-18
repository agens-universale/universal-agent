from __future__ import annotations

from pathlib import Path

from .errors import ErrorCategory, OfficeReaderError


def validate_path(
    requested: str,
    allowed_dirs: list[str],
    *,
    allow_any: bool,
) -> Path:
    real = Path(requested).resolve()
    if allow_any:
        return real
    for allowed in allowed_dirs:
        allowed_real = Path(allowed).resolve()
        if real == allowed_real or allowed_real in real.parents:
            return real
    raise OfficeReaderError(
        ErrorCategory.PATH_NOT_ALLOWED,
        path=requested,
        allowed_dirs=allowed_dirs,
    )
