import asyncio
import os
import shutil
import sys

import pytest
from gc_core.backends.factory import (
    os_backend_section,
    parse_os_backend_kind,
    resolve_os_backend,
)
from gc_core.backends.harbor import HarborBackend
from gc_core.backends.local import LocalFsBackend
from gc_core.backends.remote_shell import RemoteShellBackend
from gc_core.backends.sandbox import SandboxBackend
from gc_core.run_context import acreate_run_context


def test_parse_os_backend_defaults_to_local():
    assert parse_os_backend_kind({}) == "local"
    assert parse_os_backend_kind({"os_backend": "  LOCAL  "}) == "local"


def test_parse_os_backend_rejects_unknown():
    with pytest.raises(ValueError, match="unsupported"):
        parse_os_backend_kind({"os_backend": "s3"})


def test_parse_os_backend_harbor():
    assert parse_os_backend_kind({"os_backend": "harbor"}) == "harbor"
    assert parse_os_backend_kind({}) == "local"


def test_resolve_local():
    be = resolve_os_backend({})
    assert isinstance(be, LocalFsBackend)


def test_resolve_harbor_requires_runtime():
    with pytest.raises(ValueError, match="environment and loop"):
        resolve_os_backend({"os_backend": "harbor"})


def test_resolve_override_wins():
    sentinel = object()
    assert resolve_os_backend({}, override=sentinel) is sentinel  # type: ignore[arg-type]


def test_resolve_harbor_with_env_and_loop():
    env = object()
    loop = asyncio.new_event_loop()
    try:
        be = resolve_os_backend(
            {"os_backend": "harbor", "harbor_exec_init": "bashrc"},
            environment=env,
            loop=loop,
        )
        assert isinstance(be, HarborBackend)
        assert be.exec_init == "bashrc"
    finally:
        loop.close()


def test_resolve_harbor_rejects_non_string_exec_init():
    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(ValueError, match="harbor_exec_init"):
            resolve_os_backend(
                {"os_backend": "harbor", "harbor_exec_init": ["bashrc"]},
                environment=object(),
                loop=loop,
            )
    finally:
        loop.close()


async def test_create_run_context_leaves_local_bare_where_recording_is_off():
    """Вне TUI без включённой записи правок (`agent_edits.record`) Local не
    оборачивается: трекер зовёт `git status` вокруг каждой команды зря."""
    ctx = await acreate_run_context(
        {"backend_api_mode": "chat_completions", "save_logs": False}
    )
    assert isinstance(ctx.os_backend, LocalFsBackend)
    assert getattr(ctx, "is_tui", False) is False


async def test_create_run_context_tui_wraps_tracking():
    from gc_core.agent_edits.tracking import TrackingFsBackend

    ctx = await acreate_run_context(
        {"backend_api_mode": "chat_completions", "save_logs": False},
        is_tui=True,
    )
    assert isinstance(ctx.os_backend, TrackingFsBackend)
    assert isinstance(ctx.os_backend.inner, LocalFsBackend)
    assert ctx.is_tui is True


async def test_create_run_context_explicit_override():
    sentinel = object()
    ctx = await acreate_run_context(
        {"save_logs": False},
        os_backend=sentinel,  # type: ignore[arg-type]
        is_tui=True,
    )
    # explicit override: не оборачиваем Tracking
    assert ctx.os_backend is sentinel
    assert ctx.workspace.backend is sentinel


async def test_workspace_is_assembled_once_even_when_wrapped(monkeypatch):
    """Обёртка подменяет бэкенд в готовой области, а не пересобирает её.

    Пересборка стоила бы ВТОРОЙ пробы записи в корень проекта — реальной
    записи на диск на каждом старте, при том что докстринг `ExecutionWorkspace`
    обещает ровно одну.
    """
    from gc_core import execution_workspace as ew
    from gc_core.agent_edits.tracking import TrackingFsBackend

    probes = []
    real = ew.resolve_root_writable
    monkeypatch.setattr(
        ew,
        "resolve_root_writable",
        lambda backend, root: (probes.append(root), real(backend, root))[1],
    )

    ctx = await acreate_run_context(
        {"backend_api_mode": "chat_completions", "save_logs": False},
        is_tui=True,
    )

    assert isinstance(ctx.os_backend, TrackingFsBackend)
    assert len(probes) == 1, (
        f"проба записи должна быть одна, а их {len(probes)}"
    )
    assert ctx.workspace.backend is ctx.os_backend, (
        "рабочая область обязана смотреть на ту же обёртку, что и контекст"
    )


def test_section_accepts_string_dict_and_missing():
    assert os_backend_section({}) == {"kind": "local"}
    assert os_backend_section({"os_backend": "Harbor"}) == {"kind": "harbor"}
    sec = os_backend_section(
        {"os_backend": {"kind": "docker", "docker": {"name": "b"}}}
    )
    assert sec["kind"] == "docker" and sec["docker"]["name"] == "b"


def test_parse_kind_lists_all_supported():
    for k in ("local", "harbor", "docker", "ssh", "exec", "sandbox"):
        assert parse_os_backend_kind({"os_backend": k}) == k


def test_resolve_docker_builds_remote_shell():
    be = resolve_os_backend(
        {
            "os_backend": {"kind": "docker", "docker": {"name": "box"}},
            "harbor_exec_init": "bashrc",
        }
    )
    assert isinstance(be, RemoteShellBackend)
    assert (
        be.argv_prefix == ("docker", "exec", "-i", "box")
        and be.exec_init == "bashrc"
    )


def test_resolve_docker_without_name_fails_loudly():
    with pytest.raises(ValueError, match="docker.name"):
        resolve_os_backend({"os_backend": "docker"})


def test_resolve_exec_passes_prefix_cwd_env():
    be = resolve_os_backend(
        {
            "os_backend": {
                "kind": "exec",
                "cwd": "/w",
                "env": {"A": "1"},
                "exec": {"argv_prefix": ["env"]},
            }
        }
    )
    assert be.argv_prefix == ("env",) and be.default_cwd() == "/w"


_tool = (
    sys.platform == "darwin" and shutil.which("sandbox-exec")
) or shutil.which("bwrap")


@pytest.mark.skipif(not _tool, reason="нужен bwrap / sandbox-exec")
def test_resolve_sandbox_auto_workdir(tmp_path, monkeypatch):
    # env — закэшированный синглтон: setenv не виден, подменяем сам gc_paths
    monkeypatch.setattr(
        "gc_core.backends.sandbox.gc_paths", lambda: {"state": tmp_path}
    )
    monkeypatch.chdir(tmp_path / "proj") if (
        tmp_path / "proj"
    ).mkdir() is None else None
    from gc_core.config_modules.env_vars import project_slug

    cfg = {"os_backend": {"kind": "sandbox", "sandbox": {"workdir": "auto"}}}
    be = resolve_os_backend(cfg)
    assert isinstance(be, SandboxBackend)
    # Один каталог на папку запуска: повторный запуск и продолжение треда
    # (-t) видят те же файлы, а не пустую песочницу.
    expected = str(
        tmp_path / "sandboxes" / project_slug(str(tmp_path / "proj"))
    )
    assert be.auto_workdir == expected and os.path.isdir(expected)
    assert be.default_cwd() == be.auto_workdir
    assert resolve_os_backend(cfg).auto_workdir == expected


def test_resolve_sandbox_fails_closed_without_tool(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="sandbox"):
        resolve_os_backend({"os_backend": "sandbox"})


def test_resolve_ssh_joins_argv_docker_does_not():
    ssh = resolve_os_backend(
        {"os_backend": {"kind": "ssh", "ssh": {"host": "h"}}}
    )
    docker = resolve_os_backend(
        {"os_backend": {"kind": "docker", "docker": {"name": "b"}}}
    )
    assert ssh.joins_argv is True and docker.joins_argv is False
