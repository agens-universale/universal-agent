"""@-аттач: ввод '@' открывает FilePicker; фильтрация; достройка; пара на сабмите."""
import pytest


@pytest.mark.asyncio
async def test_at_opens_file_picker(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "agent.py").write_text("x", encoding="utf-8")

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        # insert (не присваивание text) — двигает курсор в конец и постит Changed,
        # иначе курсор остаётся в (0,0) и @-сегмент не распознаётся.
        inp.insert("@age")
        await pilot.pause(0.1)
        assert scr._file_picker is not None
        assert any("agents/agent.py" in e for e in scr._file_picker._entries)


@pytest.mark.asyncio
async def test_complete_dir_keeps_picker_and_file_closes(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "agent.py").write_text("x", encoding="utf-8")

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()

        # выбрать папку agents/ → буфер @agents/, пикер остаётся
        inp.insert("@agents")
        await pilot.pause(0.1)
        await pilot.press("tab")
        await pilot.pause(0.1)
        assert inp.text == "@agents/"
        assert scr._file_picker is not None

        # выбрать файл → буфер @agents/agent.py + пробел, пикер закрыт
        await pilot.pause(0.1)
        await pilot.press("tab")
        await pilot.pause(0.1)
        assert inp.text == "@agents/agent.py "
        assert scr._file_picker is None


@pytest.mark.asyncio
async def test_submit_with_attachment_sends_pair(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    captured = {}

    class _Fake:
        async def arun(self, messages, **kw):
            captured["messages"] = list(messages)
            import threading
            self._evt = threading.Event()
            self._evt.wait(timeout=2.0)

        def stop_run(self):
            if getattr(self, "_evt", None):
                self._evt.set()

        def add_message(self, m):
            pass

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    app._executor_factory = lambda: _Fake()
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "почини @a.py"
        await pilot.press("enter")
        await pilot.pause(0.3)
        msgs = captured["messages"]
        assert msgs[0] == "почини @a.py"
        assert len(msgs) == 2
        assert msgs[1].startswith('<technical type="technical">')
        assert "(файл)" in msgs[1]


@pytest.mark.asyncio
async def test_arrows_navigate_picker_not_history(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    # несколько записей верхнего уровня, чтобы было что листать
    (tmp_path / "alpha.py").write_text("x", encoding="utf-8")
    (tmp_path / "beta.py").write_text("x", encoding="utf-8")
    (tmp_path / "gamma.py").write_text("x", encoding="utf-8")

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("@")
        await pilot.pause(0.1)
        assert scr._file_picker is not None
        first = scr._file_picker.selected_entry()

        # Down двигает выбор вниз, пикер НЕ закрывается, текст не подменяется историей
        await pilot.press("down")
        await pilot.pause(0.05)
        assert scr._file_picker is not None
        assert scr._file_picker.selected_entry() != first
        assert inp.text == "@"

        # Up возвращает выбор обратно (а не открывает историю/не закрывает пикер)
        await pilot.press("up")
        await pilot.pause(0.05)
        assert scr._file_picker is not None
        assert scr._file_picker.selected_entry() == first
        assert inp.text == "@"


@pytest.mark.asyncio
async def test_esc_closes_picker_not_clear_hint(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    (tmp_path / "alpha.py").write_text("x", encoding="utf-8")

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("@")
        await pilot.pause(0.1)
        assert scr._file_picker is not None

        await pilot.press("escape")
        await pilot.pause(0.05)
        # Esc закрыл пикер; НЕ ушёл в Esc-стейт-машину (нет clear-hint), текст цел
        assert scr._file_picker is None
        assert scr._esc_confirm_kind is None
        assert inp.text == "@"


@pytest.mark.asyncio
async def test_submit_without_attachment_single_message(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    captured = {}

    class _Fake:
        async def arun(self, messages, **kw):
            captured["messages"] = list(messages)
            import threading
            self._evt = threading.Event()
            self._evt.wait(timeout=2.0)

        def stop_run(self):
            if getattr(self, "_evt", None):
                self._evt.set()

        def add_message(self, m):
            pass

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    app._executor_factory = lambda: _Fake()
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "просто текст без вложений"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert captured["messages"] == ["просто текст без вложений"]
