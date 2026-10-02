"""Resolve ``FsBackend`` from config and runtime dependencies."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from gc_core.backends.harbor import HarborBackend
from gc_core.backends.local import LocalFsBackend
from gc_core.backends.protocol import FsBackend
from gc_core.backends.remote_shell import (
    RemoteShellBackend,
    docker_argv_prefix,
    ssh_argv_prefix,
)
from gc_core.backends.sandbox import (
    SandboxBackend,
    build_sandbox_wrapper,
    sandbox_auto_workdir,
)
from gc_core.config_modules.agent import (
    exec_output_capture_max_chars_from_config_dict,
    harbor_exec_init_from_config_dict,
    walk_output_max_chars_from_config_dict,
)
from gc_core.config_modules.env_vars import gc_paths
from gc_core.execution_workspace import launch_root_from_config

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
    run_id: Optional[str] = None,
) -> FsBackend:
    """Build the physical I/O backend for a run.

    ``override`` wins (tests) and keeps its own ``exec_output_capture_max_chars``.
    ``harbor`` requires ``environment`` and ``loop``. ``run_id`` нужен только
    ``sandbox`` с ``workdir: auto`` — имя свежего каталога рана.
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

    section = os_backend_section(config_dict)
    if kind in ("docker", "ssh", "exec"):
        return RemoteShellBackend(
            _remote_argv_prefix(kind, section),
            cwd=section.get("cwd"),
            env=section.get("env") or {},
            exec_init=harbor_exec_init_from_config_dict(config_dict),
            exec_output_capture_max_chars=cap,
            walk_output_max_chars=walk_output_max_chars_from_config_dict(
                config_dict
            ),
        )

    if kind == "sandbox":
        auto = (
            sandbox_auto_workdir(run_id or "adhoc")
            if (section.get("sandbox") or {}).get("workdir") == "auto"
            else None
        )
        root = auto or launch_root_from_config(config_dict)
        return SandboxBackend(
            wrapper=build_sandbox_wrapper(root, section),
            root=root,
            auto_workdir=auto,
            exec_output_capture_max_chars=cap,
        )

    raise ValueError(f"unsupported os_backend: {kind!r}")


def _remote_argv_prefix(kind: str, section: dict) -> list:
    """argv-префикс RemoteShellBackend; плоский dict мог обойти pydantic."""
    if kind == "docker":
        if not (section.get("docker") or {}).get("name"):
            raise ValueError(
                "os_backend.docker.name обязателен при kind=docker"
            )
        return docker_argv_prefix(section)
    if kind == "ssh":
        if not (section.get("ssh") or {}).get("host"):
            raise ValueError("os_backend.ssh.host обязателен при kind=ssh")
        return ssh_argv_prefix(
            section, control_dir=gc_paths()["state"] / "ssh"
        )
    prefix = list((section.get("exec") or {}).get("argv_prefix") or [])
    if not prefix:
        raise ValueError(
            "os_backend.exec.argv_prefix обязателен при kind=exec"
        )
    return prefix
