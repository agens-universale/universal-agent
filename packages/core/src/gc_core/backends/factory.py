"""Resolve ``FsBackend`` from config and runtime dependencies."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from gc_core.backends.harbor import HarborBackend
from gc_core.backends.local import LocalFsBackend
from gc_core.backends.protocol import WALK_OUTPUT_MAX_CHARS, FsBackend
from gc_core.settings import runtime_of, settings_of

_SUPPORTED = frozenset({"local", "harbor"})


def parse_os_backend_kind(config_dict: dict) -> str:
    """Return normalized ``os_backend`` kind (``local`` or ``harbor``).

    Вид бэкенда выбирает встраивающий: ``RuntimeEnv.os_backend`` (None — local).
    """
    raw = runtime_of(config_dict).os_backend or "local"
    if not isinstance(raw, str) or not raw.strip():
        kind = "local"
    else:
        kind = raw.strip().lower()
    if kind not in _SUPPORTED:
        raise ValueError(
            f"unsupported os_backend: {raw!r} (expected local or harbor)"
        )
    return kind


def resolve_os_backend(
    config_dict: dict,
    *,
    environment: Optional[Any] = None,
    loop: Optional[asyncio.AbstractEventLoop] = None,
    override: Optional[FsBackend] = None,
) -> FsBackend:
    """Build the physical I/O backend for a run.

    ``override`` wins (tests) and keeps its own ``exec_output_capture_max_chars``.
    ``harbor`` requires ``environment`` and ``loop``.
    """
    if override is not None:
        return override

    agent = settings_of(config_dict).agent
    # Нормализацию (None → дефолт, нечисло и < 0 — ошибка) делает схема.
    cap = agent.exec_output_capture_max_chars
    kind = parse_os_backend_kind(config_dict)
    if kind == "local":
        return LocalFsBackend(exec_output_capture_max_chars=cap)

    if kind == "harbor":
        if environment is None or loop is None:
            raise ValueError(
                "os_backend=harbor requires environment and loop in create_run_context"
            )
        return HarborBackend(
            environment,
            loop,
            exec_output_capture_max_chars=cap,
            walk_output_max_chars=(
                agent.walk_output_max_chars
                if agent.walk_output_max_chars > 0
                else WALK_OUTPUT_MAX_CHARS
            ),
            exec_init=(agent.harbor_exec_init or "").strip() or None,
        )

    raise ValueError(f"unsupported os_backend: {kind!r}")
