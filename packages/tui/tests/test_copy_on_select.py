"""Copy-on-select / Ctrl+Shift+C: единый copy_out, без stop при пустом selection."""
from __future__ import annotations

import pytest

pytest.importorskip("textual")


@pytest.mark.asyncio
async def test_ctrl_shift_c_copies_without_stop(tmp_path, monkeypatch):
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
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "стартуй"
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert pilot.app.active_runner.is_running()

        # нет выделения → Ctrl+Shift+C не stop
        await pilot.press("ctrl+shift+c")
        await pilot.pause(0.1)
        assert stop_called["flag"] is False
        assert copied == []

        inp.text = "фрагмент"
        inp.select_all()
        await pilot.pause(0.1)
        await pilot.press("ctrl+shift+c")
        await pilot.pause(0.1)
        assert copied == ["фрагмент"]
        assert stop_called["flag"] is False


@pytest.mark.asyncio
async def test_text_selected_calls_copy_out(tmp_path, monkeypatch):
    from textual import events
    from gc_tui.app import TuiApp
    from gc_tui.services import clipboard_ops

    seen: list = []

    def fake_copy_out(app, text, *, toast=False, blocking=True):
        seen.append((text, toast, blocking))
        return True

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        monkeypatch.setattr(clipboard_ops, "copy_out", fake_copy_out)
        # patch на экземпляре через модуль, которым пользуется app — переопределим метод
        monkeypatch.setattr(
            "gc_tui.app.copy_out",
            fake_copy_out,
        )
        monkeypatch.setattr(pilot.app, "_current_selection", lambda: "выделено")
        pilot.app.post_message(events.TextSelected())
        # debounce 150ms — ждём flush
        await pilot.pause(0.25)
        assert seen == [("выделено", False, False)]  # toast=False, blocking=False


@pytest.mark.asyncio
async def test_text_selected_debounce_keeps_last(tmp_path, monkeypatch):
    from textual import events
    from gc_tui.app import TuiApp

    seen: list = []

    def fake_copy_out(app, text, *, toast=False, blocking=True):
        seen.append(text)
        return True

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        monkeypatch.setattr("gc_tui.app.copy_out", fake_copy_out)
        seq = {"n": 0}
        texts = ["один", "два"]

        def _sel():
            i = min(seq["n"], len(texts) - 1)
            return texts[i]

        monkeypatch.setattr(pilot.app, "_current_selection", _sel)
        pilot.app.post_message(events.TextSelected())
        seq["n"] = 1
        pilot.app.post_message(events.TextSelected())
        await pilot.pause(0.25)
        assert seen == ["два"]


@pytest.mark.asyncio
async def test_text_selected_empty_noop(tmp_path, monkeypatch):
    from textual import events
    from gc_tui.app import TuiApp

    seen: list = []

    def fake_copy_out(app, text, *, toast=False, blocking=True):
        seen.append(text)
        return True

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        monkeypatch.setattr("gc_tui.app.copy_out", fake_copy_out)
        monkeypatch.setattr(pilot.app, "_current_selection", lambda: "")
        pilot.app.post_message(events.TextSelected())
        await pilot.pause(0.25)
        assert seen == []


@pytest.mark.asyncio
async def test_clipboard_payload_appends_diff_cite(tmp_path, monkeypatch):
    """С DiffViewerScreen в буфер уходит суффикс [diff: path]."""
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffViewerScreen

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
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
        monkeypatch.setattr(pilot.app, "_current_selection", lambda: "+hello")
        assert pilot.app._clipboard_payload() == "+hello\n[diff: src/x.py]"
