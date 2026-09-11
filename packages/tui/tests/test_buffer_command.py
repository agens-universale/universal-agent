"""Шаг 4: /buffer команда + F7 binding + action_buffer + _dispatch_command.

SPEC 2026-07-25 §3.1 B5/B10:
- /buffer или F7 открывают CopyBufferScreen.
- F7 priority=True (перебивает TextArea f7=select_all).
- Повторный F7 на открытом буфере — no-op.
- Нет активной сессии — короткое сообщение, экран не открываем.
"""
from __future__ import annotations

import pytest

pytest.importorskip("textual")


class _MockStore:
    def __init__(self, existing_buffer: str = "") -> None:
        self._existing = existing_buffer
        self.writes: list = []

    def get_task_meta(self, task_id, system=None):
        return {"tui": {"copy_buffer": self._existing}}

    def update_tui_meta(self, task_id, patch, system=None):
        self.writes.append((task_id, dict(patch)))


def test_buffer_command_registered():
    """B5: /buffer в реестре команд с hotkey F7."""
    from gc_tui.commands import build_default_registry

    reg = build_default_registry()
    cmd = reg.lookup("buffer")
    assert cmd is not None
    assert cmd.name == "buffer"
    assert "F7" in cmd.hotkey


def test_f7_binding_priority():
    """B5: F7 в BINDINGS с priority=True (иначе TextArea select_all перебивает)."""
    from gc_tui.app import TuiApp

    f7 = [b for b in TuiApp.BINDINGS if b.key == "f7"]
    assert len(f7) == 1
    assert f7[0].action == "buffer"
    assert f7[0].priority is True


@pytest.mark.asyncio
async def test_action_buffer_opens_screen(tmp_path):
    """B5: есть сессия → открывается CopyBufferScreen."""
    from gc_tui.app import TuiApp
    from gc_tui.screens.copy_buffer import CopyBufferScreen

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        store = _MockStore(existing_buffer="старый")
        pilot.app.screen._store = store
        pilot.app.screen._active_task_id = "T1"
        pilot.app.action_buffer()
        await pilot.pause()
        assert isinstance(pilot.app.screen, CopyBufferScreen)


@pytest.mark.asyncio
async def test_action_buffer_noop_if_already_open(tmp_path):
    """B5: повторный F7 на открытом буфере — no-op (второй push не нужен)."""
    from gc_tui.app import TuiApp
    from gc_tui.screens.copy_buffer import CopyBufferScreen

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        store = _MockStore(existing_buffer="")
        pilot.app.screen._store = store
        pilot.app.screen._active_task_id = "T2"
        pilot.app.action_buffer()
        await pilot.pause()
        assert isinstance(pilot.app.screen, CopyBufferScreen)
        depth_before = len(pilot.app.screen_stack)
        pilot.app.action_buffer()  # повторный — no-op
        await pilot.pause()
        assert len(pilot.app.screen_stack) == depth_before


@pytest.mark.asyncio
async def test_action_buffer_no_session_notify(tmp_path):
    """B5: нет активной сессии → короткое сообщение, экран не открываем."""
    from gc_tui.app import TuiApp
    from gc_tui.screens.copy_buffer import CopyBufferScreen

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        pilot.app.screen._active_task_id = None
        pilot.app.action_buffer()
        await pilot.pause()
        assert not isinstance(pilot.app.screen, CopyBufferScreen)


@pytest.mark.asyncio
async def test_dispatch_command_buffer(tmp_path):
    """B5: /buffer через _dispatch_command открывает экран."""
    from gc_tui.app import TuiApp
    from gc_tui.screens.copy_buffer import CopyBufferScreen

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        store = _MockStore(existing_buffer="")
        pilot.app.screen._store = store
        pilot.app.screen._active_task_id = "T3"
        await pilot.app.screen._dispatch_command("buffer")
        await pilot.pause()
        assert isinstance(pilot.app.screen, CopyBufferScreen)





@pytest.mark.asyncio
async def test_push_session_buffer_from_diff_screen(tmp_path):
    """B2: Ctrl+C с выделением на DiffViewerScreen дописывает в буфер сессии.

    Store/task_id берутся из базового TraceScreen (как action_buffer),
    а не из self.screen — у diff/view экранов их нет.
    """
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffViewerScreen

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        store = _MockStore(existing_buffer="старый")
        pilot.app.screen._store = store
        pilot.app.screen._active_task_id = "T1"
        # поверх TraceScreen — DiffViewerScreen (без _store/_active_task_id)
        viewer = DiffViewerScreen(
            title="дифф · M · src/x.py",
            content="+hello\n",
            path="src/x.py",
            on_approve=lambda: None,
            on_undo=lambda: None,
            on_approve_all=lambda: None,
            on_undo_all=lambda: None,
        )
        await pilot.app.push_screen(viewer)
        await pilot.pause()
        # сверху теперь diff-экран, у него нет _store/_active_task_id
        assert not hasattr(pilot.app.screen, "_active_task_id") or getattr(
            pilot.app.screen, "_active_task_id", None
        ) is None
        pilot.app._push_session_copy_buffer("фрагмент")
        await pilot.pause()
        assert store.writes == [("T1", {"copy_buffer": "фрагмент\n\nстарый"})]



@pytest.mark.asyncio
async def test_buffer_ctrl_a_selects_all(tmp_path):
    """B11: Ctrl+A на экране буфера выделяет весь текст.

    Штатный TextArea биндит ctrl+a → cursor_line_start, а select_all на f7,
    который перехватывается app-уровнем (action_buffer priority=True).
    Нужен явный ctrl+a → select_all в BufferTextArea.
    """
    from gc_tui.app import TuiApp
    from gc_tui.screens.copy_buffer import CopyBufferScreen

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        store = _MockStore(existing_buffer="первая\nвторая\nтретья")
        pilot.app.screen._store = store
        pilot.app.screen._active_task_id = "T1"
        pilot.app.action_buffer()
        await pilot.pause()
        assert isinstance(pilot.app.screen, CopyBufferScreen)
        ta = pilot.app.screen.query_one("#buffer_text")
        # До выделения — пустой selection
        assert ta.selection is None or ta.selection.end == ta.selection.start
        await pilot.press("ctrl+a")
        await pilot.pause()
        # После Ctrl+A — выделен весь текст
        assert ta.selection is not None
        assert ta.selection.start == (0, 0)
        assert ta.selection.end[0] >= 2  # до последней строки


@pytest.mark.asyncio
async def test_save_strips_whitespace_only_to_empty(tmp_path):
    """B11: при сохранении чистый whitespace (переносы/пробелы) сжимается до пустой.

    Если в поле остались только переносы строк и пробелы — сохраняем пустую
    строку (= очистили буфер), не пишем висячий whitespace.
    """
    from gc_tui.app import TuiApp
    from gc_tui.screens.copy_buffer import CopyBufferScreen

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        store = _MockStore(existing_buffer="старый")
        pilot.app.screen._store = store
        pilot.app.screen._active_task_id = "T1"
        pilot.app.action_buffer()
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, CopyBufferScreen)
        ta = screen.query_one("#buffer_text")
        # Только переносы и пробелы
        ta.text = "  \n\n   \n  \t \n"
        screen.action_save_and_close()
        await pilot.pause()
        # Записали пустую строку (= очистили), а не висячий whitespace
        assert store.writes == [("T1", {"copy_buffer": ""})]


@pytest.mark.asyncio
async def test_save_preserves_real_text_with_strip_edges(tmp_path):
    """B11: реальный текст сохраняется, краевые пустые строки/хвосты пробелов стрипаются."""
    from gc_tui.app import TuiApp
    from gc_tui.screens.copy_buffer import CopyBufferScreen

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        store = _MockStore(existing_buffer="")
        pilot.app.screen._store = store
        pilot.app.screen._active_task_id = "T1"
        pilot.app.action_buffer()
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, CopyBufferScreen)
        ta = screen.query_one("#buffer_text")
        ta.text = "\n\n  реальный текст  \n\n  ещё строка  \n\n"
        screen.action_save_and_close()
        await pilot.pause()
        saved = store.writes[0][1]["copy_buffer"]
        assert saved == "реальный текст\n\n  ещё строка"
