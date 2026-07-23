"""Resolve ``FsBackend`` from config and runtime dependencies."""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from backends.protocol import FsBackend
from backends.local import LocalFsBackend
from backends.harbor import HarborBackend

_SUPPORTED = frozenset({"local", "harbor"})


def parse_os_backend_kind(config_dict: dict) -> str:
    """Return normalized ``os_backend`` kind (``local`` or ``harbor``)."""
    raw = config_dict.get("os_backend", "local")
    if not isinstance(raw, str) or not raw.strip():
        kind = "local"
    else:
        kind = raw.strip().lower()
    if kind not in _SUPPORTED:
        raise ValueError(f"unsupported os_backend: {raw!r} (expected local or harbor)")
    return kind


def resolve_os_backend(
    config_dict: dict,
    *,
    environment: Optional[Any] = None,
    loop: Optional[asyncio.AbstractEventLoop] = None,
    override: Optional[FsBackend] = None,
) -> FsBackend:
    """Build the physical I/O backend for a run.

    ``override`` wins (tests). ``harbor`` requires ``environment`` and ``loop``.
    """
    if override is not None:
        return override

    kind = parse_os_backend_kind(config_dict)
    if kind == "local":
        return LocalFsBackend()

    if kind == "harbor":
        if environment is None or loop is None:
            raise ValueError(
                "os_backend=harbor requires environment and loop in create_run_context"
            )

        return HarborBackend(environment, loop)

    raise ValueError(f"unsupported os_backend: {kind!r}")
