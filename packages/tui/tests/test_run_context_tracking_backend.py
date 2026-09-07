"""Diff-журнал включает TUI, а не ядро — и включает по-настоящему.

История. Обёртку `LocalFsBackend` → `TrackingFsBackend` делал сам
`create_run_context` под `if is_tui`, и ради неё ядро импортировало
`gc_tui.diff_tui.tracking`. Это была ЕДИНСТВЕННАЯ зависимость `gc_core` от
фронтенда; 27.08.2026 она снята — три действия переехали в
`gc_tui.diff_tui.tracking.attach_to_context`, который зовёт `TuiApp`.

Проверка осталась в наборе TUI (переехала сюда 18.08.2026 из набора ядра), но
сменила сторону: раньше стерегла, что ядро оборачивает, теперь — что ядро НЕ
оборачивает, а TUI оборачивает.
"""
from gc_core.backends.local import LocalFsBackend
from gc_core.run_context import create_run_context

_CFG = {"backend_api_mode": "chat_completions", "save_logs": False}


def test_core_leaves_the_backend_alone():
    """Ядро контекст только строит. Не декорация, а инвариант: ядро не знает про diff."""
    from gc_tui.diff_tui.tracking import TrackingFsBackend

    ctx = create_run_context(_CFG, is_tui=True)

    assert isinstance(ctx.os_backend, LocalFsBackend)
    assert not isinstance(ctx.os_backend, TrackingFsBackend)
    assert ctx.is_tui is True


def test_attach_wraps_backend_and_rebuilds_workspace():
    """`attach_to_context` делает ОБА действия.

    Пересборка workspace — половина, которую легко забыть: без неё инструменты
    продолжат писать мимо журнала через старый backend, а diff останется пустым,
    и ни один тест обёртки этого не заметит.
    """
    from gc_tui.diff_tui.tracking import TrackingFsBackend, attach_to_context

    ctx = create_run_context(_CFG, is_tui=True)
    inner = ctx.os_backend
    attach_to_context(ctx, _CFG)

    assert isinstance(ctx.os_backend, TrackingFsBackend)
    assert ctx.os_backend.inner is inner
    assert ctx.workspace.backend is ctx.os_backend


def test_attach_is_idempotent():
    """Повторный вызов ничего не меняет: обёртка поверх обёртки — потерянный журнал."""
    from gc_tui.diff_tui.tracking import attach_to_context

    ctx = create_run_context(_CFG, is_tui=True)
    attach_to_context(ctx, _CFG)
    first = ctx.os_backend
    attach_to_context(ctx, _CFG)

    assert ctx.os_backend is first


def test_app_turns_the_journal_on():
    """`TuiApp` обязан звать `attach_to_context` при сборке исполнителя.

    Сторож на разрыв проводки: обе функции выше останутся зелёными, даже если строку
    вызова из `_build_real_executor` убрать, — а diff-экран тихо опустеет. Смотрим на
    исходник, потому что поднимать настоящий исполнитель здесь несоразмерно дорого.
    """
    import inspect

    from gc_tui.app import TuiApp

    src = inspect.getsource(TuiApp._build_real_executor)
    assert "attach_to_context" in src, (
        "TuiApp перестал включать diff-журнал — F3 останется пустым"
    )
