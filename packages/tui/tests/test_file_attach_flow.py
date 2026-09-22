"""@-аттач: ввод '@' открывает FilePicker; фильтрация; достройка; пара на сабмите."""
import pytest

from file_search_harness import settled


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
        await pilot.pause()
        await settled(scr._file_search)
        await pilot.pause()
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
        await pilot.pause()
        await settled(scr._file_search)
        await pilot.pause()
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


@pytest.mark.asyncio
async def test_fuzzy_search_runs_off_ui_thread(tmp_path, monkeypatch):
    """Поиск по индексу не исполняется в UI-потоке: на огромном проекте он вешал
    event loop на минуты (не работал даже Ctrl-C). Считает его поток сеанса —
    демон, а не воркер из общего пула цикла событий."""
    pytest.importorskip("textual")
    import threading
    from gc_tui.app import TuiApp
    from gc_tui.services import file_index

    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "agent.py").write_text("x", encoding="utf-8")
    seen = []
    real = file_index.PathIndex.search

    def spy(self, query, **kw):
        thread = threading.current_thread()
        seen.append((thread is threading.main_thread(), thread.name, thread.daemon))
        return real(self, query, **kw)

    monkeypatch.setattr(file_index.PathIndex, "search", spy)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("@age")
        await pilot.pause()
        await settled(scr._file_search)
        await pilot.pause()
        # Поток сеанса, не пул цикла событий; демон — иначе выход повис бы в threading._shutdown.
        assert seen == [(False, "file-search", True)]
        assert any("agents/agent.py" in e for e in scr._file_picker._entries)


@pytest.mark.asyncio
async def test_index_is_reused_between_keystrokes_and_dropped_on_close(tmp_path):
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
        inp.insert("@ag")
        await pilot.pause()
        await settled(scr._file_search)
        first = scr._path_index
        assert first is not None
        inp.insert("e")
        await pilot.pause()
        await settled(scr._file_search)
        assert scr._path_index is first
        await pilot.press("escape")
        await pilot.pause()
        assert scr._path_index is None


@pytest.mark.asyncio
async def test_late_search_result_does_not_reopen_picker(tmp_path, monkeypatch):
    """Поиск ещё в полёте (пикер не открыт), а @-сегмент уже закончился:
    опоздавший результат пикер не открывает."""
    pytest.importorskip("textual")
    import threading
    from gc_tui.app import TuiApp
    from gc_tui.services import file_index

    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "agent.py").write_text("x", encoding="utf-8")
    release = threading.Event()
    real = file_index.PathIndex.search

    def blocked(self, query, **kw):
        release.wait(timeout=5)
        return real(self, query, **kw)

    monkeypatch.setattr(file_index.PathIndex, "search", blocked)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("@age")
        await pilot.pause()
        search = scr._file_search
        assert scr._file_picker is None          # результат ещё не пришёл
        inp.insert(" ")                          # сегмент закончился
        await pilot.pause()
        release.set()
        await settled(search)
        await pilot.pause()
        assert scr._file_picker is None


@pytest.mark.asyncio
async def test_truncated_index_shows_note_in_picker(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.services import file_index

    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "agent.py").write_text("x", encoding="utf-8")
    real = file_index.PathIndex.search

    def truncating(self, query, **kw):
        out = real(self, query, **kw)
        self.truncated = True
        return out

    monkeypatch.setattr(file_index.PathIndex, "search", truncating)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("@age")
        await pilot.pause()
        await settled(scr._file_search)
        await pilot.pause()
        picker = scr._file_picker
        assert picker._truncated is True
        assert len(picker.query(".palette-keys")) == 2
        assert picker.desired_height == len(picker._entries) + 2 + 4
        # Человек пошёл по папкам ('/' в запросе) — совет выполнен, строка уходит.
        inp.insert("nts/ag")                     # @agents/ag
        await pilot.pause()
        await settled(scr._file_search)
        await pilot.pause()
        assert scr._file_picker._truncated is False


@pytest.mark.asyncio
async def test_result_of_another_picker_session_is_dropped(tmp_path):
    """Результат принадлежит сеансу пикера (его PathIndex): поиск, начатый в
    прошлом сеансе (другой корень, закрыли-открыли), список не подменяет — даже
    если текст запроса совпал."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.services.file_index import PathIndex

    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "agent.py").write_text("x", encoding="utf-8")
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("@age")
        await pilot.pause()
        await settled(scr._file_search)
        await pilot.pause()
        before = list(scr._file_picker._entries)
        scr._on_file_search_done(PathIndex(str(tmp_path / "other")), "age", ["zzz.py"], False)
        await pilot.pause()
        assert scr._file_picker._entries == before


@pytest.mark.asyncio
async def test_failed_search_is_logged_and_does_not_crash(tmp_path, monkeypatch, caplog):
    """Сбой поиска не роняет TUI и не молчит: в лог уходит warning с корнем и
    запросом, пикер просто не открывается."""
    pytest.importorskip("textual")
    import logging
    from gc_tui.app import TuiApp
    from gc_tui.services import file_index

    def boom(self, query, **kw):
        raise RuntimeError("index exploded")

    monkeypatch.setattr(file_index.PathIndex, "search", boom)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    with caplog.at_level(logging.WARNING, logger="gc_tui.services.file_search"):
        async with app.run_test() as pilot:
            scr = pilot.app.screen
            scr._project_root = lambda: str(tmp_path)
            inp = pilot.app.query_one("#cmdinput")
            inp.focus()
            inp.insert("@age")
            await pilot.pause()
            await settled(scr._file_search)
            await pilot.pause()
            assert scr._file_picker is None
            assert pilot.app.is_running
    assert any("file search failed" in r.getMessage() and "'age'" in r.getMessage()
               for r in caplog.records)


@pytest.mark.asyncio
async def test_tab_on_stale_list_does_not_complete_wrong_path(tmp_path, monkeypatch):
    """Поиск в полёте, на экране ещё листинг прежнего запроса: Tab не должен
    достраивать путь из него — иначе «@main» молча превращается в «@aaa/»."""
    pytest.importorskip("textual")
    import threading
    from gc_tui.app import TuiApp
    from gc_tui.services import file_index

    (tmp_path / "aaa").mkdir()
    (tmp_path / "qqtarget.py").write_text("x", encoding="utf-8")
    release = threading.Event()
    real = file_index.PathIndex.search

    def blocked(self, query, **kw):
        release.wait(timeout=5)
        return real(self, query, **kw)

    monkeypatch.setattr(file_index.PathIndex, "search", blocked)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("@")                      # синхронный листинг корня (папки сверху)
        await pilot.pause()
        stale = scr._file_picker.selected_entry()
        assert stale is not None and stale != "qqtarget.py"
        inp.insert("qqtar")                  # поиск ушёл в фон и заблокирован
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert inp.text == "@qqtar"          # устаревший список не применён
        release.set()
        await settled(scr._file_search)
        await pilot.pause()
        await pilot.press("tab")             # список свежий — достройка работает
        await pilot.pause()
        assert inp.text == "@qqtarget.py "


@pytest.mark.asyncio
async def test_late_result_does_not_overwrite_directory_listing(tmp_path, monkeypatch):
    """«@agents» ищется в фоне, человек дописал «/» — показан живой листинг
    папки; опоздавший результат поиска его не перетирает."""
    pytest.importorskip("textual")
    import threading
    from gc_tui.app import TuiApp
    from gc_tui.services import file_index

    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "agent.py").write_text("x", encoding="utf-8")
    (tmp_path / "agents_old.py").write_text("x", encoding="utf-8")
    release = threading.Event()
    real = file_index.PathIndex.search

    def blocked(self, query, **kw):
        release.wait(timeout=5)
        return real(self, query, **kw)

    monkeypatch.setattr(file_index.PathIndex, "search", blocked)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("@agents")
        await pilot.pause()
        search = scr._file_search
        inp.insert("/")                      # синхронный листинг agents/
        await pilot.pause()
        assert scr._file_picker._entries == ["agents/agent.py"]
        release.set()
        await settled(search)
        await pilot.pause()
        assert scr._file_picker._entries == ["agents/agent.py"]


@pytest.mark.asyncio
async def test_truncated_index_with_no_matches_keeps_picker_with_note(tmp_path, monkeypatch):
    """Главный сценарий подсказки: файла нет в обрезанном индексе, совпадений 0 —
    пикер не молчит, а показывает «нет совпадений» и строку про путь. Без
    обрезания пустая выдача по-прежнему прячет пикер."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.services import file_index

    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "agent.py").write_text("x", encoding="utf-8")
    real = file_index.PathIndex.search

    def truncating(self, query, **kw):
        out = real(self, query, **kw)
        self.truncated = True
        return out

    monkeypatch.setattr(file_index.PathIndex, "search", truncating)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("@zzz")
        await pilot.pause()
        await settled(scr._file_search)
        await pilot.pause()
        picker = scr._file_picker
        assert picker is not None
        assert picker._entries == []
        assert picker._truncated is True
        assert len(picker.query(".palette-keys")) == 2
        footer = picker.query_one(".palette-footer")
        assert footer.region.height == 4          # margin+подсказка, margin+клавиши
        assert footer.region.bottom <= picker.region.bottom


@pytest.mark.asyncio
async def test_git_timeout_is_remembered_for_next_picker_session(tmp_path, monkeypatch):
    """Первый «@» дождался таймаута git — следующий сеанс пикера для того же
    корня git уже не зовёт (иначе в огромном репозитории каждый «@» ждёт заново)."""
    pytest.importorskip("textual")
    import subprocess
    from gc_tui.app import TuiApp
    from gc_tui.services import file_index

    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "agent.py").write_text("x", encoding="utf-8")
    git_calls = []

    def fake_git_ls_files(root, max_paths):
        git_calls.append(root)
        raise subprocess.TimeoutExpired(cmd="git", timeout=5)

    monkeypatch.setattr(file_index, "_git_ls_files", fake_git_ls_files)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("@age")
        await pilot.pause()
        await settled(scr._file_search)
        await pilot.pause()
        assert len(git_calls) == 1
        assert any("agents/agent.py" in e for e in scr._file_picker._entries)
        await pilot.press("escape")              # конец сеанса
        await pilot.pause()
        inp.insert("n")                          # @agen — новый сеанс, новый индекс
        await pilot.pause()
        await settled(scr._file_search)
        await pilot.pause()
        assert len(git_calls) == 1               # git второй раз не звали
        assert any("agents/agent.py" in e for e in scr._file_picker._entries)


@pytest.mark.asyncio
async def test_slow_search_shows_searching_row_then_results(tmp_path, monkeypatch):
    """Поиск дольше порога: вместо прежнего списка — строка «ищу по проекту…»;
    пришёл результат — она сменяется списком."""
    pytest.importorskip("textual")
    import threading
    from gc_tui.app import TuiApp
    from gc_tui.services import file_index

    (tmp_path / "aaa").mkdir()
    (tmp_path / "qqtarget.py").write_text("x", encoding="utf-8")
    release = threading.Event()
    real = file_index.PathIndex.search

    def blocked(self, query, **kw):
        release.wait(timeout=5)
        return real(self, query, **kw)

    monkeypatch.setattr(file_index.PathIndex, "search", blocked)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        scr._FILE_SEARCH_NOTICE_DELAY = 0.01
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("@")
        await pilot.pause()
        assert scr._file_picker._entries                 # листинг корня
        inp.insert("qqtar")
        await pilot.pause(0.2)
        assert scr._file_picker is not None
        assert scr._file_picker._entries == []
        assert scr._file_picker._searching is True
        release.set()
        await settled(scr._file_search)
        await pilot.pause()
        assert scr._file_picker._searching is False
        assert scr._file_picker._entries == ["qqtarget.py"]


@pytest.mark.asyncio
async def test_fast_search_never_shows_searching_row(tmp_path):
    """Обычный проект: результат приходит раньше порога — «ищу…» не мелькает."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    (tmp_path / "qqtarget.py").write_text("x", encoding="utf-8")
    seen = []
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        real_pending = scr._apply_file_entries

        def spy(entries, *, truncated, searching=False):
            seen.append(searching)
            return real_pending(entries, truncated=truncated, searching=searching)

        scr._apply_file_entries = spy
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("@qqtar")
        await pilot.pause()
        await settled(scr._file_search)
        await pilot.pause(0.5)                          # порог 0.3 с давно прошёл
        assert seen == [False]
        assert scr._file_picker._entries == ["qqtarget.py"]


@pytest.mark.asyncio
async def test_escape_cancels_search_in_flight_before_picker_is_shown(tmp_path, monkeypatch):
    """Esc, пока поиск в полёте и пикер ещё не показан, отменяет сеанс:
    опоздавший результат пикер не открывает; текст остаётся в поле."""
    pytest.importorskip("textual")
    import threading
    from gc_tui.app import TuiApp
    from gc_tui.services import file_index

    (tmp_path / "qqtarget.py").write_text("x", encoding="utf-8")
    release = threading.Event()
    real = file_index.PathIndex.search

    def blocked(self, query, **kw):
        release.wait(timeout=5)
        return real(self, query, **kw)

    monkeypatch.setattr(file_index.PathIndex, "search", blocked)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("@qqtar")                             # одним вводом: листинга не было
        await pilot.pause()
        search = scr._file_search
        assert scr._file_picker is None
        await pilot.press("escape")
        await pilot.pause()
        assert scr._file_query is None
        release.set()
        await settled(search)
        await pilot.pause(0.5)                           # и порог индикатора прошёл
        assert scr._file_picker is None
        assert inp.text == "@qqtar"


@pytest.mark.asyncio
async def test_escape_closes_open_overlay_before_cancelling_file_search(tmp_path, monkeypatch):
    """Поиск файлов в полёте, а поверх открыт другой оверлей (ввод одним событием
    сменился с «@…» на «/skill …»): Esc принадлежит открытому оверлею — отмена
    поиска не должна его перехватывать."""
    pytest.importorskip("textual")
    import threading
    from gc_tui.app import TuiApp
    from gc_tui.services import file_index

    (tmp_path / "qqtarget.py").write_text("x", encoding="utf-8")
    release = threading.Event()
    real = file_index.PathIndex.search

    def blocked(self, query, **kw):
        release.wait(timeout=5)
        return real(self, query, **kw)

    monkeypatch.setattr(file_index.PathIndex, "search", blocked)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("@qqtar")                      # поиск ушёл в фон и заблокирован
        await pilot.pause()
        search = scr._file_search
        inp.text = "/skill zz"                    # одним событием: открылся skill-пикер
        await pilot.pause()
        assert scr._skill_picker is not None
        await pilot.press("escape")
        await pilot.pause()
        assert scr._skill_picker is None          # Esc достался открытому оверлею
        release.set()
        await settled(search)
        await pilot.pause()


@pytest.mark.asyncio
async def test_truncated_note_stays_for_slash_query_without_such_dir(tmp_path, monkeypatch):
    """«nodir/ag»: в запросе есть '/', но папки nodir под корнем нет — это поиск
    по обрезанному индексу, подсказка о неполном поиске обязана остаться."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.services import file_index

    (tmp_path / "src" / "nodir").mkdir(parents=True)
    (tmp_path / "src" / "nodir" / "agent.py").write_text("x", encoding="utf-8")
    real = file_index.PathIndex.search

    def truncating(self, query, **kw):
        out = real(self, query, **kw)
        self.truncated = True
        return out

    monkeypatch.setattr(file_index.PathIndex, "search", truncating)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("@nodir/ag")
        await pilot.pause()
        await settled(scr._file_search)
        await pilot.pause()
        assert scr._file_picker._entries == ["src/nodir/agent.py"]
        assert scr._file_picker._truncated is True


@pytest.mark.asyncio
async def test_enter_waits_while_file_search_in_flight(tmp_path, monkeypatch):
    """Enter, пока поиск в полёте, не отправляет сообщение — ни при строке
    «ищу по проекту…», ни до неё; после результата достраивает путь."""
    pytest.importorskip("textual")
    import threading
    from gc_tui.app import TuiApp
    from gc_tui.services import file_index

    (tmp_path / "qqtarget.py").write_text("x", encoding="utf-8")
    sent = []

    class _Fake:
        async def arun(self, messages, **kw):
            sent.append(list(messages))

        def stop_run(self):
            pass

        def add_message(self, m):
            pass

    release = threading.Event()
    real = file_index.PathIndex.search

    def blocked(self, query, **kw):
        release.wait(timeout=5)
        return real(self, query, **kw)

    monkeypatch.setattr(file_index.PathIndex, "search", blocked)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    app._executor_factory = lambda: _Fake()
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("@qqtar")
        await pilot.pause()
        assert scr._file_picker is None          # пикера ещё нет: порог «ищу…» не прошёл
        await pilot.press("enter")                      # пикера ещё нет
        await pilot.pause(0.5)
        assert scr._file_picker._searching is True      # показана «ищу по проекту…»
        await pilot.press("enter")
        await pilot.pause()
        assert sent == []
        assert inp.text == "@qqtar"
        release.set()
        await settled(scr._file_search)
        await pilot.pause()
        await pilot.press("enter")                      # результат пришёл — достройка
        await pilot.pause()
        assert inp.text == "@qqtarget.py "
        assert sent == []


@pytest.mark.asyncio
async def test_escape_then_enter_sends_text_as_is(tmp_path, monkeypatch):
    """Выход из ожидания: Esc отменяет поиск, следующий Enter отправляет набранное."""
    pytest.importorskip("textual")
    import threading
    from gc_tui.app import TuiApp
    from gc_tui.services import file_index

    (tmp_path / "qqtarget.py").write_text("x", encoding="utf-8")
    sent = []

    class _Fake:
        async def arun(self, messages, **kw):
            sent.append(list(messages))

        def stop_run(self):
            pass

        def add_message(self, m):
            pass

    release = threading.Event()
    real = file_index.PathIndex.search

    def blocked(self, query, **kw):
        release.wait(timeout=5)
        return real(self, query, **kw)

    monkeypatch.setattr(file_index.PathIndex, "search", blocked)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    app._executor_factory = lambda: _Fake()
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("привет @qqtar")
        await pilot.pause()
        search = scr._file_search
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert sent and sent[0][0] == "привет @qqtar"
        release.set()
        await settled(search)
        await pilot.pause()


@pytest.mark.asyncio
async def test_stale_search_skips_filtering_after_index_build(tmp_path, monkeypatch):
    """Пока собирается индекс, накопились поиски по промежуточному вводу: после
    сборки фильтруется только актуальный запрос, устаревшие выходят без фильтра."""
    pytest.importorskip("textual")
    import threading
    from gc_tui.app import TuiApp
    from gc_tui.services import file_index

    (tmp_path / "qqtarget.py").write_text("x", encoding="utf-8")
    release = threading.Event()
    real_build = file_index.PathIndex.build
    real_search = file_index.PathIndex.search
    searched = []

    def slow_build(self):
        release.wait(timeout=5)
        return real_build(self)

    def spy_search(self, query, **kw):
        searched.append(query)
        return real_search(self, query, **kw)

    monkeypatch.setattr(file_index.PathIndex, "build", slow_build)
    monkeypatch.setattr(file_index.PathIndex, "search", spy_search)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("@qq")
        await pilot.pause()
        first = scr._file_search
        inp.insert("tar")                                # @qqtar
        await pilot.pause()
        release.set()
        await settled(first)
        await settled(scr._file_search)
        await pilot.pause()
        assert searched == ["qqtar"]
        assert scr._file_picker._entries == ["qqtarget.py"]


@pytest.mark.asyncio
async def test_git_timeout_is_remembered_even_if_search_was_cancelled(tmp_path, monkeypatch):
    """Esc во время первой сборки: сеанс закрыт, поиск устарел и не фильтрует, но
    сборка доходит до конца и модуль индекса запоминает таймаут git — следующий
    «@» не ждёт его заново. Интеграционный сторож: память пишет поток сборки,
    экран в этом не участвует."""
    pytest.importorskip("textual")
    import subprocess
    import threading
    from gc_tui.app import TuiApp
    from gc_tui.services import file_index

    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "agent.py").write_text("x", encoding="utf-8")
    release = threading.Event()

    def fake_git_ls_files(root, max_paths):
        release.wait(timeout=5)
        raise subprocess.TimeoutExpired(cmd="git", timeout=5)

    monkeypatch.setattr(file_index, "_git_ls_files", fake_git_ls_files)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("@age")
        await pilot.pause()
        search = scr._file_search
        await pilot.press("escape")
        await pilot.pause()
        release.set()
        await settled(search)
        await pilot.pause()
        assert str(tmp_path) in file_index._slow_git_roots
        assert scr._file_picker is None


@pytest.mark.asyncio
async def test_escape_during_index_build_does_not_start_second_build(tmp_path, monkeypatch):
    """Esc во время первой сборки и новый ввод: сборка одна — новый сеанс
    подхватывает идущую, а не запускает вторую параллельно."""
    pytest.importorskip("textual")
    import threading
    from gc_tui.app import TuiApp
    from gc_tui.services import file_index

    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "agent.py").write_text("x", encoding="utf-8")
    release = threading.Event()
    builds = []
    real = file_index._all_paths

    def slow(root, max_paths, **kw):
        builds.append(root)
        release.wait(timeout=5)
        return real(root, max_paths, **kw)

    monkeypatch.setattr(file_index, "_all_paths", slow)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("@age")
        await pilot.pause()
        first = scr._file_search
        await pilot.press("escape")
        await pilot.pause()
        inp.insert("n")                                  # @agen — новый сеанс
        await pilot.pause()
        release.set()
        await settled(first)
        await settled(scr._file_search)
        await pilot.pause()
        assert builds == [str(tmp_path)]
        assert any("agents/agent.py" in e for e in scr._file_picker._entries)


@pytest.mark.asyncio
async def test_index_finished_after_close_is_not_reused_by_next_session(tmp_path, monkeypatch):
    """Индекс, оставленный строиться при закрытии, достроился, а сеанса так и не
    было: следующий сеанс собирает свежий индекс, а не берёт залежавшийся."""
    pytest.importorskip("textual")
    import threading
    from gc_tui.app import TuiApp
    from gc_tui.services import file_index

    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "agent.py").write_text("x", encoding="utf-8")
    release = threading.Event()
    real = file_index._all_paths

    def slow(root, max_paths, **kw):
        release.wait(timeout=5)
        return real(root, max_paths, **kw)

    monkeypatch.setattr(file_index, "_all_paths", slow)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("@age")
        await pilot.pause()
        search = scr._file_search
        kept = scr._path_index
        await pilot.press("escape")
        await pilot.pause()
        assert scr._path_index is kept                   # строится — оставлен
        release.set()
        await settled(search)
        await pilot.pause()
        assert kept.built is True
        inp.insert("n")                                  # новый сеанс после достройки
        await pilot.pause()
        await settled(scr._file_search)
        await pilot.pause()
        assert scr._path_index is not kept


@pytest.mark.asyncio
async def test_switching_to_skill_input_ends_file_search_session(tmp_path, monkeypatch):
    """Поиск файлов в полёте, ввод одним событием сменился на «/skill …» (история,
    вставка): опоздавший результат не должен открыть файловый пикер поверх команды."""
    pytest.importorskip("textual")
    import threading
    from gc_tui.app import TuiApp
    from gc_tui.services import file_index

    (tmp_path / "qqtarget.py").write_text("x", encoding="utf-8")
    release = threading.Event()
    real = file_index.PathIndex.search

    def blocked(self, query, **kw):
        release.wait(timeout=5)
        return real(self, query, **kw)

    monkeypatch.setattr(file_index.PathIndex, "search", blocked)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("@qqtar")
        await pilot.pause()
        search = scr._file_search
        inp.text = "/skill zz"
        await pilot.pause()
        assert scr._file_query is None
        release.set()
        await settled(search)
        await pilot.pause(0.5)
        assert scr._file_picker is None


@pytest.mark.asyncio
async def test_switching_to_arg_picker_input_ends_file_search_session(tmp_path, monkeypatch):
    """То же для команды с вариантами аргумента: пикер аргументов и файловый
    не должны оказаться открыты одновременно."""
    pytest.importorskip("textual")
    import threading
    from gc_tui.app import TuiApp
    from gc_tui.services import file_index

    (tmp_path / "qqtarget.py").write_text("x", encoding="utf-8")
    release = threading.Event()
    real = file_index.PathIndex.search

    def blocked(self, query, **kw):
        release.wait(timeout=5)
        return real(self, query, **kw)

    monkeypatch.setattr(file_index.PathIndex, "search", blocked)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("@qqtar")
        await pilot.pause()
        search = scr._file_search
        inp.text = "/export j"
        await pilot.pause()
        assert scr._arg_picker is not None
        assert scr._file_query is None
        release.set()
        await settled(search)
        await pilot.pause(0.5)
        assert scr._file_picker is None


@pytest.mark.asyncio
async def test_big_directory_listing_is_capped_with_note(tmp_path):
    """Папка на 150 файлов: показано 100 и подпись; дописал часть имени — находится
    и то, что в первые 100 не попало."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    (tmp_path / "many").mkdir()
    for i in range(150):
        (tmp_path / "many" / ("f%03d.txt" % i)).write_text("x", encoding="utf-8")
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("@many/")
        await pilot.pause()
        picker = scr._file_picker
        assert len(picker._entries) == 100
        assert picker._note_text() == "показано 100 из 150 · допишите часть имени"
        assert len(picker.query(".palette-keys")) == 2
        inp.insert("f149")
        await pilot.pause()
        await settled(scr._file_search)
        await pilot.pause()
        assert scr._file_picker._entries == ["many/f149.txt"]
        assert scr._file_picker._note_text() == ""


@pytest.mark.asyncio
async def test_slow_git_memo_expires_and_git_is_tried_again(tmp_path, monkeypatch):
    """Память о медленном git не вечная: после срока следующий сеанс пикера снова
    пробует git."""
    pytest.importorskip("textual")
    import subprocess
    from gc_tui.app import TuiApp
    from gc_tui.services import file_index

    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "agent.py").write_text("x", encoding="utf-8")
    git_calls = []

    def timed_out(root, max_paths):
        git_calls.append(root)
        raise subprocess.TimeoutExpired(cmd="git", timeout=5)

    monkeypatch.setattr(file_index, "_git_ls_files", timed_out)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("@age")
        await pilot.pause()
        await settled(scr._file_search)
        await pilot.pause()
        assert len(git_calls) == 1
        await pilot.press("escape")
        await pilot.pause()
        monkeypatch.setattr(file_index, "_SLOW_GIT_RETRY_AFTER", 0.0)   # срок вышел
        inp.insert("n")                                  # новый сеанс
        await pilot.pause()
        await settled(scr._file_search)
        await pilot.pause()
        assert len(git_calls) == 2


@pytest.mark.asyncio
async def test_typing_during_index_build_starts_no_new_threads(tmp_path, monkeypatch):
    """Пока собирается индекс, быстрый набор букв не плодит потоки: их считает один
    поток сеанса, после сборки фильтруется только последний запрос."""
    pytest.importorskip("textual")
    import threading
    from gc_tui.app import TuiApp
    from gc_tui.services import file_index

    (tmp_path / "qqtarget.py").write_text("x", encoding="utf-8")
    release = threading.Event()
    real_build = file_index.PathIndex.build
    real_search = file_index.PathIndex.search
    searched = []

    def slow_build(self):
        release.wait(timeout=5)
        return real_build(self)

    def spy_search(self, query, **kw):
        searched.append(query)
        return real_search(self, query, **kw)

    monkeypatch.setattr(file_index.PathIndex, "build", slow_build)
    monkeypatch.setattr(file_index.PathIndex, "search", spy_search)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("@q")                                 # первая поисковая буква — поток сеанса
        await pilot.pause()
        search = scr._file_search
        before = set(threading.enumerate())
        for ch in "qtar":
            inp.insert(ch)
            await pilot.pause()
        assert set(threading.enumerate()) - before == set()
        assert scr._file_search is search
        release.set()
        await settled(search)
        await pilot.pause()
        assert searched == ["qqtar"]
        assert scr._file_picker._entries == ["qqtarget.py"]


@pytest.mark.asyncio
async def test_app_exit_ends_file_search_thread(tmp_path):
    """Экран снят (выход из приложения) — сеанс поиска закрыт, его поток завершился,
    а не висит простаивающим."""
    pytest.importorskip("textual")
    import threading
    from gc_tui.app import TuiApp

    (tmp_path / "qqtarget.py").write_text("x", encoding="utf-8")
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    before = set(threading.enumerate())
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("@qqtar")
        await pilot.pause()
        await settled(scr._file_search)
        started = [t for t in set(threading.enumerate()) - before if t.name == "file-search"]
        assert len(started) == 1
    started[0].join(5)
    assert not started[0].is_alive()
