"""Шаг 3: CopyBufferScreen — TextArea, wrap, paste B8, Esc=save if changed.

SPEC 2026-07-25 §3.1 B6/B7/B8: экран буфера сессии; Esc сохраняет то, что в поле
(пусто = очистили), без изменений — на диск не пишем; paste OS-first; копирование
не дописывает буфер сам в себя (маркер _is_copy_buffer_screen).
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


@pytest.mark.asyncio
async def test_copy_buffer_screen_chrome(tmp_path):
    """Канон ViewerScreen: шапка с заголовком, футер-подсказка, палитровая рамка

    (не дефолтная Textual — она рисуется box-глифами, которые на Windows
    консоли превращаются в мусорные квадратики по периметру, см. дефект)."""
    from textual.widgets import Static
    from gc_tui.app import TuiApp
    from gc_tui.screens.copy_buffer import BufferTextArea, CopyBufferScreen

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        store = _MockStore()
        await pilot.app.push_screen(CopyBufferScreen(store=store, task_id="TCHROME"))
        await pilot.pause()
        scr = pilot.app.screen
        assert "Буфер сессии" in str(scr.query_one("#header-left", Static).render())
        assert scr.query_one("#footer", Static) is not None
        ta = scr.query_one("#buffer_text", BufferTextArea)
        assert ta.styles.border_top[0] == "solid"  # не дефолт Textual (tall/…)


@pytest.mark.asyncio
async def test_copy_buffer_screen_marker(tmp_path):
    from gc_tui.screens.copy_buffer import CopyBufferScreen

    screen = CopyBufferScreen(store=_MockStore(), task_id="T1")
    assert screen._is_copy_buffer_screen is True


@pytest.mark.asyncio
async def test_copy_buffer_screen_loads_existing(tmp_path):
    from gc_tui.app import TuiApp
    from gc_tui.screens.copy_buffer import CopyBufferScreen

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        store = _MockStore(existing_buffer="старый буфер")
        await pilot.app.push_screen(CopyBufferScreen(store=store, task_id="T2"))
        await pilot.pause()
        ta = pilot.app.screen.query_one("#buffer_text")
        assert ta.text == "старый буфер"


@pytest.mark.asyncio
async def test_copy_buffer_screen_no_store_opens_empty(tmp_path):
    from gc_tui.app import TuiApp
    from gc_tui.screens.copy_buffer import CopyBufferScreen

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        await pilot.app.push_screen(CopyBufferScreen(store=None, task_id=None))
        await pilot.pause()
        ta = pilot.app.screen.query_one("#buffer_text")
        assert ta.text == ""


@pytest.mark.asyncio
async def test_esc_no_change_no_write(tmp_path):
    from gc_tui.app import TuiApp
    from gc_tui.screens.copy_buffer import CopyBufferScreen

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        store = _MockStore(existing_buffer="не меняли")
        await pilot.app.push_screen(CopyBufferScreen(store=store, task_id="T3"))
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert store.writes == []  # без изменений — на диск не пишем


@pytest.mark.asyncio
async def test_esc_with_change_saves(tmp_path):
    from gc_tui.app import TuiApp
    from gc_tui.screens.copy_buffer import CopyBufferScreen

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        store = _MockStore(existing_buffer="старый")
        await pilot.app.push_screen(CopyBufferScreen(store=store, task_id="T4"))
        await pilot.pause()
        ta = pilot.app.screen.query_one("#buffer_text")
        ta.text = "новый текст"
        await pilot.press("escape")
        await pilot.pause()
        assert store.writes == [("T4", {"copy_buffer": "новый текст"})]


@pytest.mark.asyncio
async def test_esc_empty_clears_buffer(tmp_path):
    """B7: пустой текст = очистили буфер → пишем пустую строку."""
    from gc_tui.app import TuiApp
    from gc_tui.screens.copy_buffer import CopyBufferScreen

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        store = _MockStore(existing_buffer="было")
        await pilot.app.push_screen(CopyBufferScreen(store=store, task_id="T5"))
        await pilot.pause()
        ta = pilot.app.screen.query_one("#buffer_text")
        ta.text = ""
        await pilot.press("escape")
        await pilot.pause()
        assert store.writes == [("T5", {"copy_buffer": ""})]


@pytest.mark.asyncio
async def test_save_clamps_to_max(tmp_path):
    from gc_tui.app import TuiApp
    from gc_tui.screens.copy_buffer import CopyBufferScreen
    from gc_tui.services.copy_buffer import COPY_BUFFER_MAX

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        store = _MockStore(existing_buffer="")
        await pilot.app.push_screen(CopyBufferScreen(store=store, task_id="T6"))
        await pilot.pause()
        ta = pilot.app.screen.query_one("#buffer_text")
        ta.text = "x" * (COPY_BUFFER_MAX + 500)
        await pilot.press("escape")
        await pilot.pause()
        assert len(store.writes) == 1
        saved = store.writes[0][1]["copy_buffer"]
        assert len(saved) == COPY_BUFFER_MAX


@pytest.mark.asyncio
async def test_paste_os_first(tmp_path, monkeypatch):
    """B8: paste сначала из буфера ОС, пусто — внутренний буфер TUI."""
    from gc_tui.app import TuiApp
    from gc_tui.screens.copy_buffer import CopyBufferScreen

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        monkeypatch.setattr("gc_tui.services.os_ops.read_clipboard", lambda: "из-ОС")
        store = _MockStore(existing_buffer="")
        await pilot.app.push_screen(CopyBufferScreen(store=store, task_id="T7"))
        await pilot.pause()
        ta = pilot.app.screen.query_one("#buffer_text")
        ta.action_paste()
        assert "из-ОС" in ta.text


@pytest.mark.asyncio
async def test_ctrl_shift_v_binding_pastes(tmp_path, monkeypatch):
    """B8: ctrl+shift+v привязан к paste (не только ctrl+v / bracketed paste терминала)."""
    from gc_tui.app import TuiApp
    from gc_tui.screens.copy_buffer import BufferTextArea, CopyBufferScreen

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        monkeypatch.setattr("gc_tui.services.os_ops.read_clipboard", lambda: "shift-v")
        store = _MockStore(existing_buffer="")
        await pilot.app.push_screen(CopyBufferScreen(store=store, task_id="T7b"))
        await pilot.pause()
        ta = pilot.app.screen.query_one("#buffer_text", BufferTextArea)
        keys = {b.key for b in ta.BINDINGS}
        assert "ctrl+shift+v" in keys
        await pilot.press("ctrl+shift+v")
        await pilot.pause()
        assert "shift-v" in ta.text


@pytest.mark.asyncio
async def test_paste_falls_back_to_internal(tmp_path, monkeypatch):
    """B8: OS-буфер пуст → откат к внутреннему app.clipboard."""
    from gc_tui.app import TuiApp
    from gc_tui.screens.copy_buffer import CopyBufferScreen

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        monkeypatch.setattr("gc_tui.services.os_ops.read_clipboard", lambda: "")
        # app.clipboard — read-only property, читает self._clipboard
        pilot.app._clipboard = "из-TUI"
        store = _MockStore(existing_buffer="")
        await pilot.app.push_screen(CopyBufferScreen(store=store, task_id="T8"))
        await pilot.pause()
        ta = pilot.app.screen.query_one("#buffer_text")
        ta.action_paste()
        assert "из-TUI" in ta.text
