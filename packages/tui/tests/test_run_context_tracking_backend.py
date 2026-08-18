"""В TUI-режиме ядро оборачивает бэкенд в TrackingFsBackend из gc_tui.

Проверка жила в наборе ЯДРА (os_backends/test_os_backend_factory.py) и
импортировала gc_tui.diff_tui.tracking. Утверждение проверяет поведение ЯДРА,
но подтвердить его без установленного gc-tui нельзя — значит по правилу
«верхний пакет побеждает» место проверки здесь. Переехала 18.08.2026.
"""
from gc_core.backends.local import LocalFsBackend
from gc_core.run_context import create_run_context


def test_create_run_context_tui_wraps_tracking():
    from gc_tui.diff_tui.tracking import TrackingFsBackend

    ctx = create_run_context(
        {"backend_api_mode": "chat_completions", "save_logs": False},
        is_tui=True,
    )
    assert isinstance(ctx.os_backend, TrackingFsBackend)
    assert isinstance(ctx.os_backend.inner, LocalFsBackend)
    assert ctx.is_tui is True
