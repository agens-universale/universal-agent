import asyncio

import pytest

from gc_core.run_context import acreate_run_context
from gc_core.backends.factory import parse_os_backend_kind, resolve_os_backend
from gc_core.backends.harbor import HarborBackend
from gc_core.backends.local import LocalFsBackend


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
        ew, "resolve_root_writable",
        lambda backend, root: (probes.append(root), real(backend, root))[1],
    )

    ctx = await acreate_run_context(
        {"backend_api_mode": "chat_completions", "save_logs": False}, is_tui=True,
    )

    assert isinstance(ctx.os_backend, TrackingFsBackend)
    assert len(probes) == 1, f"проба записи должна быть одна, а их {len(probes)}"
    assert ctx.workspace.backend is ctx.os_backend, (
        "рабочая область обязана смотреть на ту же обёртку, что и контекст"
    )
