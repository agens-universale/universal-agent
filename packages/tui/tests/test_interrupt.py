"""Ctrl+C: есть выделение → копировать; нет выделения → остановить run."""

import pytest


@pytest.mark.asyncio
async def test_ctrl_c_copies_when_input_has_selection(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    copied: list = []
    stop_called = {"flag": False}

    class _Fake:
        async def arun(self, messages, *, thread_id, **kw):
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
        # Копирование ловим на транспорте: OSC 52 с 2026-08-04 шлётся только в
        # SSH, на Linux и как запасной путь (см. services/clipboard_ops).
        from gc_tui.services import clipboard_ops

        monkeypatch.setattr(clipboard_ops, "write_clipboard", lambda text: copied.append(text) or True)
        # запускаем run, затем выделяем текст в поле ввода
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "выделенный текст"
        inp.select_all()
        await pilot.pause(0.1)
        await pilot.press("ctrl+c")
        # нативная запись уходит в фоновый поток — ждём наблюдаемый результат
        from gc_testkit.tui_support import wait_until

        await wait_until(pilot, lambda: copied == ["выделенный текст"], "текст ушёл в системный буфер")
        assert stop_called["flag"] is False


@pytest.mark.asyncio
async def test_ctrl_c_stops_run_when_nothing_selected(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    stop_called = {"flag": False}

    class _Fake:
        async def arun(self, messages, *, thread_id, **kw):
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
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "стартуй"
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert pilot.app.active_runner.is_running()

        # поле пустое после отправки, выделения нет → Ctrl+C останавливает
        await pilot.press("ctrl+c")
        await pilot.pause(0.3)
        assert stop_called["flag"] is True
