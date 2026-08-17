"""Шаг 2: Ctrl+C + selection → copy_out + push в буфер сессии (tui.copy_buffer).

SPEC 2026-07-25 §3.1 B2/B8: Ctrl+C копирует наружу и дописывает буфер сессии
сверху тем же payload (с cite); на экране буфера — не дописывает сам в себя.
Регресс: Ctrl+C без выделения → stop (не push).
"""
from __future__ import annotations

import pytest

pytest.importorskip("textual")


class _MockStore:
    """Мок FileTaskStore: хранит copy_buffer, записывает update_tui_meta."""

    def __init__(self, existing_buffer: str = "") -> None:
        self._existing = existing_buffer
        self.writes: list = []

    def get_task_meta(self, task_id, system=None):
        return {"tui": {"copy_buffer": self._existing}}

    def update_tui_meta(self, task_id, patch, system=None):
        self.writes.append((task_id, dict(patch)))


@pytest.mark.asyncio
async def test_push_session_copy_buffer_writes_meta(tmp_path, monkeypatch):
    """_push_session_copy_buffer читает existing и пишет prepend через update_tui_meta."""
    from gc_tui.app import TuiApp
    from gc_tui.services import clipboard_ops

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        monkeypatch.setattr(pilot.app, "copy_to_clipboard", lambda t: None)
        monkeypatch.setattr(clipboard_ops, "write_clipboard", lambda t: True)
        store = _MockStore(existing_buffer="старый")
        monkeypatch.setattr(pilot.app.screen, "_store", store, raising=False)
        monkeypatch.setattr(pilot.app.screen, "_active_task_id", "TID1", raising=False)

        pilot.app._push_session_copy_buffer("новый")

        assert store.writes == [("TID1", {"copy_buffer": "новый\n\nстарый"})]


@pytest.mark.asyncio
async def test_push_session_copy_buffer_no_store_noop(tmp_path, monkeypatch):
    from gc_tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        # store is None на старте — не должно падать
        monkeypatch.setattr(pilot.app.screen, "_store", None, raising=False)
        monkeypatch.setattr(pilot.app.screen, "_active_task_id", None, raising=False)
        pilot.app._push_session_copy_buffer("текст")  # no exception


@pytest.mark.asyncio
async def test_ctrl_c_with_selection_pushes_session_buffer(tmp_path, monkeypatch):
    """Ctrl+C + selection → copy_out + push в copy_buffer meta."""
    from gc_tui.app import TuiApp
    from gc_tui.services import clipboard_ops

    copied: list = []
    stop_called = {"flag": False}

    class _Fake:
        def run(self, messages, *, thread_id, **kw):
            import threading
            self._evt = threading.Event()
            self._evt.wait(timeout=3.0)

        def stop_run(self):
            stop_called["flag"] = True
            if getattr(self, "_evt", None):
                self._evt.set()

        def add_message(self, m):
            pass

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    app._executor_factory = lambda: _Fake()

    async with app.run_test() as pilot:
        monkeypatch.setattr(pilot.app, "copy_to_clipboard", lambda t: copied.append(t))
        monkeypatch.setattr(clipboard_ops, "write_clipboard", lambda t: True)
        store = _MockStore(existing_buffer="")
        monkeypatch.setattr(pilot.app.screen, "_store", store, raising=False)
        monkeypatch.setattr(pilot.app.screen, "_active_task_id", "TID2", raising=False)
        monkeypatch.setattr(pilot.app, "_clipboard_payload", lambda: "фрагмент")

        await pilot.press("ctrl+c")
        await pilot.pause(0.1)

        assert copied == ["фрагмент"]
        assert store.writes == [("TID2", {"copy_buffer": "фрагмент"})]
        assert stop_called["flag"] is False  # не стопаем


@pytest.mark.asyncio
async def test_ctrl_c_without_selection_stops_no_push(tmp_path, monkeypatch):
    """Регресс: Ctrl+C без выделения → stop, copy_buffer не пишется."""
    from gc_tui.app import TuiApp
    from gc_tui.services import clipboard_ops

    stop_called = {"flag": False}

    class _Fake:
        def run(self, messages, *, thread_id, **kw):
            import threading
            self._evt = threading.Event()
            self._evt.wait(timeout=3.0)

        def stop_run(self):
            stop_called["flag"] = True
            if getattr(self, "_evt", None):
                self._evt.set()

        def add_message(self, m):
            pass

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    app._executor_factory = lambda: _Fake()

    async with app.run_test() as pilot:
        monkeypatch.setattr(clipboard_ops, "write_clipboard", lambda t: True)
        store = _MockStore(existing_buffer="")
        monkeypatch.setattr(pilot.app.screen, "_store", store, raising=False)
        monkeypatch.setattr(pilot.app.screen, "_active_task_id", "TID3", raising=False)
        monkeypatch.setattr(pilot.app, "_clipboard_payload", lambda: "")

        inp = pilot.app.query_one("#cmdinput")
        inp.text = "стартуй"
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert pilot.app.active_runner.is_running()

        await pilot.press("ctrl+c")
        await pilot.pause(0.2)

        assert stop_called["flag"] is True
        assert store.writes == []  # copy_buffer не тронут


@pytest.mark.asyncio
async def test_push_session_buffer_blocked_on_copy_buffer_screen(tmp_path, monkeypatch):
    """B8: когда CopyBufferScreen on top — push в сессию не происходит."""
    from textual.screen import Screen
    from gc_tui.app import TuiApp
    from gc_tui.services import clipboard_ops

    class _MarkerScreen(Screen):
        """Screen-маркер: имитирует CopyBufferScreen (без TextArea)."""
        _is_copy_buffer_screen = True

        def __init__(self, store):
            super().__init__()
            self._store = store
            self._active_task_id = "TID4"

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        monkeypatch.setattr(pilot.app, "copy_to_clipboard", lambda t: None)
        monkeypatch.setattr(clipboard_ops, "write_clipboard", lambda t: True)
        store = _MockStore(existing_buffer="старый")
        marker = _MarkerScreen(store)
        await pilot.app.push_screen(marker)
        await pilot.pause()

        # напрямую: guard должен увидеть _is_copy_buffer_screen и не писать
        pilot.app._push_session_copy_buffer("выделение")
        assert store.writes == []  # в буфер сессии не пишем
