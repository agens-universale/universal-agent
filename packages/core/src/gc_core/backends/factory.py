"""Resolve ``FsBackend`` from settings and runtime dependencies."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from gc_core.backends.harbor import HarborBackend
from gc_core.backends.local import LocalFsBackend
from gc_core.backends.protocol import WALK_OUTPUT_MAX_CHARS, FsBackend
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
from gc_core.config_modules.env_vars import gc_paths
from gc_core.execution_workspace import launch_root_from_config
from gc_core.settings import runtime_of, settings_of

_SUPPORTED = frozenset({"local", "harbor", "docker", "ssh", "exec", "sandbox"})


def os_backend_section(settings: Any) -> dict:
    """Секция ``os_backend`` словарём с нормализованным ``kind``.

    Источник — ``settings.os_backend`` (проверенная схема). ``RuntimeEnv.os_backend``
    — переопределение вида встраивающим кодом (Harbor-адаптер ставит ``harbor``).
    ``settings`` — всё, что принимают ``settings_of``/``runtime_of``.
    """
    section = settings_of(settings).os_backend.model_dump()
    override = runtime_of(settings).os_backend
    if isinstance(override, str) and override.strip():
        section["kind"] = override.strip().lower()
    section["kind"] = str(section.get("kind") or "local").strip().lower()
    return section


def _reject_option_like_values(section: dict) -> None:
    """Сырой dict мимо pydantic: значение с ведущим `-` уходит в argv отдельным
    словом и читается клиентом как опция (`-oProxyCommand=…`, `--privileged`)."""
    ssh = section.get("ssh") or {}
    docker = section.get("docker") or {}
    for label, value in (
        ("ssh.host", ssh.get("host")),
        ("ssh.user", ssh.get("user")),
        ("docker.name", docker.get("name")),
        ("docker.user", docker.get("user")),
        ("docker.binary", docker.get("binary")),
    ):
        if value and str(value).startswith("-"):
            raise ValueError(
                f"os_backend.{label} не может начинаться с '-': {value!r}"
            )


def ssh_control_dir() -> Path:
    """Короткий каталог для ControlPath: unix-сокет ограничен ~104 байтами
    (macOS), а ssh добавляет к пути временный суффикс. ``<state>/ssh`` с длинным
    логином или своим GC_HOME не влезал — все операции падали."""
    if os.name == "nt":
        return gc_paths()["state"] / "ssh"
    path = Path(tempfile.gettempdir() if sys.platform != "darwin" else "/tmp")
    path = path / f"gc-ssh-{os.getuid()}"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def parse_os_backend_kind(settings: Any) -> str:
    """Нормализованный ``kind`` секции ``os_backend`` (с переопределением
    ``RuntimeEnv.os_backend``)."""
    kind = os_backend_section(settings)["kind"]
    if kind not in _SUPPORTED:
        raise ValueError(
            f"unsupported os_backend: {kind!r} (expected one of {sorted(_SUPPORTED)})"
        )
    return kind


def resolve_os_backend(
    settings: Any,
    *,
    environment: Optional[Any] = None,
    loop: Optional[asyncio.AbstractEventLoop] = None,
    override: Optional[FsBackend] = None,
) -> FsBackend:
    """Build the physical I/O backend for a run.

    ``override`` wins (tests) and keeps its own ``exec_output_capture_max_chars``.
    ``harbor`` requires ``environment`` and ``loop``. ``settings`` — anything
    ``settings_of``/``runtime_of`` accept (context, ``LoadedSettings``).
    """
    if override is not None:
        return override

    agent = settings_of(settings).agent
    # Нормализацию (None → дефолт, нечисло и < 0 — ошибка) делает схема.
    cap = agent.exec_output_capture_max_chars
    kind = parse_os_backend_kind(settings)
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
            runtime_probe_ttl_sec=agent.runtime_probe_ttl_sec,
        )

    section = os_backend_section(settings)
    if kind in ("docker", "ssh", "exec"):
        return RemoteShellBackend(
            _remote_argv_prefix(kind, section),
            cwd=section.get("cwd"),
            env=section.get("env") or {},
            exec_init=(agent.harbor_exec_init or "").strip() or None,
            exec_output_capture_max_chars=cap,
            walk_output_max_chars=(
                agent.walk_output_max_chars
                if agent.walk_output_max_chars > 0
                else WALK_OUTPUT_MAX_CHARS
            ),
            joins_argv=kind == "ssh",
            runtime_probe_ttl_sec=agent.runtime_probe_ttl_sec,
        )

    if kind == "sandbox":
        # Слаг — от launch_root конфига, а не от cwd процесса: TUI резолвит
        # симлинки, явный launch_root может быть не там, где запущен процесс.
        configured_root = launch_root_from_config(settings)
        auto = (
            sandbox_auto_workdir(configured_root)
            if (section.get("sandbox") or {}).get("workdir") == "auto"
            else None
        )
        root = auto or configured_root
        return SandboxBackend(
            wrapper=build_sandbox_wrapper(root, section),
            root=root,
            auto_workdir=auto,
            exec_output_capture_max_chars=cap,
        )

    raise ValueError(f"unsupported os_backend: {kind!r}")


def _remote_argv_prefix(kind: str, section: dict) -> list:
    """argv-префикс RemoteShellBackend (секция уже прошла схему; проверка
    ведущего ``-`` — на случай переопределения мимо неё)."""
    _reject_option_like_values(section)
    if kind == "docker":
        if not (section.get("docker") or {}).get("name"):
            raise ValueError(
                "os_backend.docker.name обязателен при kind=docker"
            )
        return docker_argv_prefix(section)
    if kind == "ssh":
        if not (section.get("ssh") or {}).get("host"):
            raise ValueError("os_backend.ssh.host обязателен при kind=ssh")
        return ssh_argv_prefix(section, control_dir=ssh_control_dir())
    prefix = list((section.get("exec") or {}).get("argv_prefix") or [])
    if not prefix:
        raise ValueError(
            "os_backend.exec.argv_prefix обязателен при kind=exec"
        )
    return prefix
