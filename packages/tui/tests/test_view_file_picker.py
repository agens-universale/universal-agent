"""/view <query>: авто-открытие файлового пикера (Command.arg_file)."""

import pytest

from file_search_harness import settled


def _mk(tmp_path):
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "agent.py").write_text("x", encoding="utf-8")


@pytest.mark.asyncio
async def test_view_space_opens_file_picker(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    _mk(tmp_path)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("/view age")
        await pilot.pause()
        await settled(scr._file_search)
        await pilot.pause()
        assert scr._file_picker is not None
        assert any("agents/agent.py" in e for e in scr._file_picker._entries)


@pytest.mark.asyncio
async def test_command_without_arg_file_does_not_open_picker(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    _mk(tmp_path)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("/status age")  # у /status нет arg_file
        await pilot.pause(0.1)
        assert scr._file_picker is None


@pytest.mark.asyncio
async def test_at_inside_view_line_stays_file_arg_picker(tmp_path):
    """@ внутри /view-строки — просто текст фильтра, НЕ @-пикер:
    состояние 3b стоит раньше @-детекта. Пикер остаётся (file-arg),
    фильтр '@...' сужает список до пустого."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    _mk(tmp_path)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("/view @age")
        await pilot.pause()
        await settled(scr._file_search)
        await pilot.pause()
        # Ключевое: @-пикер НЕ перехватил (он нашёл бы agents/agent.py по 'age'
        # и был бы открыт); file-arg query '@age' совпадений не имеет →
        # пикер скрыт (пустой ящик «нет совпадений» не показываем).
        assert scr._file_picker is None


@pytest.mark.asyncio
async def test_view_complete_dir_drills_in_file_closes(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    _mk(tmp_path)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("/view agents")
        await pilot.pause()
        await settled(scr._file_search)
        await pilot.pause()
        await pilot.press("tab")  # папка -> drill-in
        await pilot.pause(0.1)
        assert inp.text == "/view agents/"
        assert scr._file_picker is not None
        await pilot.press("tab")  # файл -> достроить и закрыть
        await pilot.pause(0.1)
        assert inp.text == "/view agents/agent.py "
        assert scr._file_picker is None


@pytest.mark.asyncio
async def test_view_escape_closes_picker_keeps_line(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    _mk(tmp_path)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("/view age")
        await pilot.pause()
        await settled(scr._file_search)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert scr._file_picker is None
        assert inp.text == "/view age"


@pytest.mark.asyncio
async def test_view_at_prefixed_filename_completes_file_arg_not_at_branch(
    tmp_path,
):
    """Имя файла начинается с '@' — Tab должен достроить его через file-arg
    ветку (приоритетнее @-сегмента, симметрично state 3b < @-детект в
    on_text_area_changed), а не через старую @-ветку (та задвоила бы
    префикс '@')."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    _mk(tmp_path)
    (tmp_path / "@agents.py").write_text("x", encoding="utf-8")
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("/view @age")
        await pilot.pause()
        await settled(scr._file_search)
        await pilot.pause()
        assert scr._file_picker is not None
        await pilot.press("tab")
        await pilot.pause(0.1)
        assert inp.text == "/view @agents.py "
        assert scr._file_picker is None


@pytest.mark.asyncio
async def test_view_enter_no_matches_submits_command(tmp_path):
    """Абсолютный/внешний путь без fuzzy-совпадений: пустой пикер скрыт,
    Enter исполняет /view как обычный сабмит (спека: «абсолютные/внешние
    пути по-прежнему набираются руками»), а не молча съедается."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.file_view import FileViewerScreen

    _mk(tmp_path)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("/view zzz/nope.txt")
        await pilot.pause()
        await settled(scr._file_search)
        await pilot.pause()
        assert scr._file_picker is None  # совпадений нет → пикер скрыт
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert isinstance(pilot.app.screen, FileViewerScreen)


@pytest.mark.asyncio
async def test_view_tab_cursor_left_of_arg_is_noop(tmp_path):
    """Курсор внутри имени команды (левее зоны аргумента) — Tab не должен
    достраивать/искажать строку; пикер остаётся открытым как есть."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    _mk(tmp_path)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("/view age")
        await pilot.pause()
        await settled(scr._file_search)
        await pilot.pause()
        assert scr._file_picker is not None
        inp.cursor_location = (0, 3)
        await pilot.press("tab")
        await pilot.pause(0.1)
        assert inp.text == "/view age"
        assert scr._file_picker is not None


@pytest.mark.asyncio
async def test_backspace_to_bare_command_closes_file_picker(tmp_path):
    """Стирание пробела и хвоста query обратно до голой '/view' возвращает
    в состояние 1 (командная палитра) — FilePicker, открытый состоянием 3b,
    должен закрыться (симметрично state 1b для '.')."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    _mk(tmp_path)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("/view age")
        await pilot.pause()
        await settled(scr._file_search)
        await pilot.pause()
        assert scr._file_picker is not None
        search = scr._file_search
        for _ in range(4):
            await pilot.press("backspace")
        await pilot.pause(0.1)
        await settled(search)
        await pilot.pause()
        assert scr._file_picker is None


@pytest.mark.asyncio
async def test_empty_matches_hide_picker_and_it_returns(tmp_path):
    """Совпадений нет → пикер скрыт (никакого ящика «нет совпадений»);
    появились совпадения → пикер сам возвращается."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    _mk(tmp_path)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("/view zzz")
        await pilot.pause()
        await settled(scr._file_search)
        await pilot.pause()
        assert scr._file_picker is None  # скрыт, не «нет совпадений»
        for _ in range(3):
            await pilot.press("backspace")  # /view zzz -> /view
        await pilot.pause(0.1)
        inp.insert("age")  # /view age — снова есть матчи
        await pilot.pause()
        await settled(scr._file_search)
        await pilot.pause()
        assert scr._file_picker is not None


@pytest.mark.asyncio
async def test_at_segment_empty_matches_hide_picker(tmp_path):
    """То же для @-аттача: '@zzz' — пикер скрыт, а не пустой ящик."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    _mk(tmp_path)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("@zzz")
        await pilot.pause()
        await settled(scr._file_search)
        await pilot.pause()
        assert scr._file_picker is None


@pytest.mark.asyncio
async def test_hint_has_keys_margin_and_honest_desired_height(tmp_path):
    """Hint FilePicker — докнутый footer (.palette-footer) с воздухом (.palette-keys),
    desired_height включает строку margin (шов main.py::_sync_trace_max_height:
    недосчёт = рамка за экраном)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    _mk(tmp_path)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("/view ")
        await pilot.pause(0.1)
        picker = scr._file_picker
        assert picker is not None
        hint = picker.query(".palette-keys")
        assert len(hint) == 1
        assert picker.query_one(".palette-footer").query(
            ".palette-keys"
        )  # hint внутри докнутого подвала
        assert picker.desired_height == len(picker.children) + 3
        assert hint[0].region.bottom <= picker.region.bottom


@pytest.mark.asyncio
async def test_hint_visible_on_open_with_long_list_no_scroll(tmp_path):
    """Регрессия c0622c9a: длинный список (> высоты клэмпа) — hint-строка видна СРАЗУ
    при открытии, без скролла (докнутый .palette-footer, а не последний FLOW-ребёнок,
    которого упирает max-height и хоронит за скроллом)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    (tmp_path / "agents").mkdir()
    for i in range(45):
        (tmp_path / "agents" / f"agent{i:03d}.py").write_text(
            "x", encoding="utf-8"
        )
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test(size=(80, 24)) as pilot:
        scr = pilot.app.screen
        scr._project_root = lambda: str(tmp_path)
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert("/view agents/agent")
        await pilot.pause()
        await settled(scr._file_search)
        # Клэмп оверлея ставит _sync_trace_max_height (call_after_refresh + poll-тик
        # экрана ~0.25 с): ассерты ниже про раскладку, ждём дольше одного тика.
        await pilot.pause(0.3)
        picker = scr._file_picker
        assert picker is not None
        assert len(picker._entries) > 20  # список длиннее клэмпа
        assert (
            picker.region.height < picker.desired_height
        )  # клэмп реально сработал — скролл включён
        hint = picker.query(".palette-keys")
        assert len(hint) == 1
        assert hint[0].region.bottom <= 24  # на экране
        assert (
            hint[0].region.bottom <= picker.region.bottom
        )  # внутри рамки пикера, видна без скролла
