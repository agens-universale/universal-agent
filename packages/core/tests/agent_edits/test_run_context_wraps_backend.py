"""Журнал правок включает ЯДРО, и включает по-настоящему.

История. До 27.08.2026 обёртку `LocalFsBackend` → `TrackingFsBackend` делал
`create_run_context` под `if is_tui`, и ради неё ядро импортировало
`gc_tui.diff_tui.tracking` — единственная зависимость `gc_core` от фронтенда.
Её сняли, перенеся три действия в `attach_to_context` на стороне TUI, и журнал
стал привилегией TUI.

Ветка правок вернула обёртку в ядро, но уже без нарушения границ: подсистема
`gc_core.agent_edits` — своя, ядру не нужно знать про diff-экран. Взамен журнал
работает во ВСЕХ режимах: правки из CLI и сервера попадают в журнал треда так
же, как из TUI.
"""
from gc_core.agent_edits.tracking import TrackingFsBackend
from gc_core.backends.local import LocalFsBackend
from gc_core.run_context import acreate_run_context

_CFG = {"backend_api_mode": "chat_completions", "save_logs": False}


async def test_core_wraps_the_backend_outside_tui_too():
    """Журнал — не привилегия TUI: CLI и сервер пишут в него так же."""
    ctx = await acreate_run_context(_CFG, is_tui=False)

    assert isinstance(ctx.os_backend, TrackingFsBackend)
    assert isinstance(ctx.os_backend.inner, LocalFsBackend)
    assert ctx.is_tui is False


async def test_workspace_points_at_the_wrapper():
    """Пересборка workspace — половина, которую легко забыть.

    Без неё инструменты продолжат писать мимо журнала через старый backend,
    экран правок останется пустым, и ни один тест самой обёртки этого не
    заметит.
    """
    ctx = await acreate_run_context(_CFG, is_tui=True)

    assert isinstance(ctx.os_backend, TrackingFsBackend)
    assert ctx.workspace.backend is ctx.os_backend


async def test_explicit_backend_is_left_alone():
    """Переданный извне backend ядро не оборачивает — так тесты и harbor берут свой."""
    given = LocalFsBackend()
    ctx = await acreate_run_context(_CFG, is_tui=True, os_backend=given)

    assert ctx.os_backend is given
