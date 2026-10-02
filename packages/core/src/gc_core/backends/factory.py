"""Resolve ``FsBackend`` from config and runtime dependencies."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from gc_core.backends.harbor import HarborBackend
from gc_core.backends.local import LocalFsBackend
from gc_core.backends.protocol import FsBackend
from gc_core.config_modules.agent import (
    exec_output_capture_max_chars_from_config_dict,
    harbor_exec_init_from_config_dict,
    walk_output_max_chars_from_config_dict,
)

_SUPPORTED = frozenset({"local", "harbor", "docker", "ssh", "exec", "sandbox"})


def os_backend_section(config_dict: dict) -> dict:
    """Нормализовать ``config_dict['os_backend']`` к словарю с ``kind``.

    Принимает отсутствие ключа, голую строку (старые конфиги, Harbor-адаптер)
    и секцию из ``flatten_app_config``.
    """
    raw = config_dict.get("os_backend", "local")
    if raw is None:
        return {"kind": "local"}
    if isinstance(raw, str):
        return {"kind": raw.strip().lower() or "local"}
    if isinstance(raw, dict):
        section = dict(raw)
        kind = section.get("kind")
        section["kind"] = (
            (str(kind).strip().lower() or "local") if kind else "local"
        )
        return section
    raise ValueError(
        f"unsupported os_backend: {raw!r} (expected str or section)"
    )


def parse_os_backend_kind(config_dict: dict) -> str:
    """Нормализованный ``kind`` секции ``os_backend``."""
    kind = os_backend_section(config_dict)["kind"]
    if kind not in _SUPPORTED:
        raise ValueError(
            f"unsupported os_backend: {kind!r} (expected one of {sorted(_SUPPORTED)})"
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

    cap = exec_output_capture_max_chars_from_config_dict(config_dict)
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
            walk_output_max_chars=walk_output_max_chars_from_config_dict(
                config_dict
            ),
            exec_init=harbor_exec_init_from_config_dict(config_dict),
        )

    raise NotImplementedError(f"os_backend={kind} is wired in a later task")
