import asyncio

import pytest

from run_context import create_run_context
from tools.backends.factory import parse_os_backend_kind, resolve_os_backend
from tools.backends.harbor import HarborBackend
from tools.backends.local import LocalFsBackend


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
        be = resolve_os_backend({"os_backend": "harbor"}, environment=env, loop=loop)
        assert isinstance(be, HarborBackend)
    finally:
        loop.close()


def test_create_run_context_sets_local_os_backend():
    ctx = create_run_context({"backend_api_mode": "chat_completions", "save_logs": False})
    assert isinstance(ctx.os_backend, LocalFsBackend)
    assert getattr(ctx, "is_tui", False) is False


def test_create_run_context_tui_wraps_tracking():
    from diff_tui.tracking import TrackingFsBackend

    ctx = create_run_context(
        {"backend_api_mode": "chat_completions", "save_logs": False},
        is_tui=True,
    )
    assert isinstance(ctx.os_backend, TrackingFsBackend)
    assert isinstance(ctx.os_backend.inner, LocalFsBackend)
    assert ctx.is_tui is True


def test_create_run_context_explicit_override():
    sentinel = object()
    ctx = create_run_context(
        {"save_logs": False},
        os_backend=sentinel,  # type: ignore[arg-type]
        is_tui=True,
    )
    # explicit override: не оборачиваем Tracking
    assert ctx.os_backend is sentinel
    assert ctx.workspace.backend is sentinel
