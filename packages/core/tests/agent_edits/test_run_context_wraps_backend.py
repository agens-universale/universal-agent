"""Журнал правок включает ЯДРО, и включает по-настоящему.

История. До 27.08.2026 обёртку `LocalFsBackend` → `TrackingFsBackend` делал
`create_run_context` под `if is_tui`, и ради неё ядро импортировало
`gc_tui.diff_tui.tracking` — единственная зависимость `gc_core` от фронтенда.
Её сняли, перенеся три действия в `attach_to_context` на стороне TUI, и журнал
стал привилегией TUI.

Ветка правок вернула обёртку в ядро, но уже без нарушения границ: подсистема
`gc_core.agent_edits` — своя, ядру не нужно знать про diff-экран. Пишет журнал
тот режим, которому он включён (`agent_edits.record`): TUI — по умолчанию, CLI и
сервер — по конфигу. Где запись выключена, обёртки вокруг команд нет вовсе.
"""

from gc_core.agent_edits.tracking import TrackingFsBackend
from gc_core.backends.local import LocalFsBackend
from gc_core.run_context import acreate_run_context

_CFG = {"backend_api_mode": "chat_completions", "save_logs": False}


async def test_outside_tui_the_backend_is_not_wrapped_by_default():
    """CLI без включённой записи — ни одного лишнего слоя вокруг команд: обёртка
    зовёт `git status` до и после каждой команды, а журнал там никто не смотрит."""
    ctx = await acreate_run_context({**_CFG, "run_mode": "cli"}, is_tui=False)

    assert isinstance(ctx.os_backend, LocalFsBackend)
    assert ctx.is_tui is False


async def test_cli_with_recording_enabled_wraps_the_backend():
    """Запись включена CLI в конфиге — обёртка стоит, как в TUI."""
    cfg = {**_CFG, "run_mode": "cli", "agent_edits": {"record": {"cli": True}}}
    ctx = await acreate_run_context(cfg, is_tui=False)

    assert isinstance(ctx.os_backend, TrackingFsBackend)
    assert isinstance(ctx.os_backend.inner, LocalFsBackend)


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
