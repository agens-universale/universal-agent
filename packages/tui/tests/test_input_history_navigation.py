"""TraceScreen integration с InputHistory: ↑/↓, draft, reset."""

import pytest


@pytest.mark.asyncio
async def test_traceScreen_initializes_history_fields():
    """TraceScreen.__init__ заводит поля _history/_history_index/_history_draft."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        assert hasattr(scr, "_history")
        assert hasattr(scr, "_history_index")
        assert hasattr(scr, "_history_draft")
        assert scr._history_index is None
        assert scr._history_draft is None


@pytest.mark.asyncio
async def test_history_initialized_on_attach(tmp_path):
    """После attach_active_task — _history создан как InputHistory(project_dir)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.services.input_history import InputHistory

    async with TuiApp(
        threads_dir=str(tmp_path / "threads")
    ).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task("task-1")
        await pilot.pause()
        assert isinstance(scr._history, InputHistory)


@pytest.mark.asyncio
async def test_history_available_in_empty_state(tmp_path):
    """↑ в свежем empty-state (без attach) подставляет запись per-project истории:
    project_dir известен со старта (threads_dir.parent), attach не требуется."""
    pytest.importorskip("textual")
    import json
    from pathlib import Path

    from gc_tui.app import TuiApp

    project_dir = Path(tmp_path)
    threads_dir = project_dir / "threads"
    threads_dir.mkdir()
    (project_dir / "input_history.jsonl").write_text(
        json.dumps(
            {"ts": "2026-06-10T00:00:00+00:00", "text": "прошлая реплика"},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    async with TuiApp(threads_dir=str(threads_dir)).run_test() as pilot:
        scr = pilot.app.screen
        inp = scr.query_one("#cmdinput")
        inp.focus()
        await pilot.press("up")
        await pilot.pause()
        assert inp.text == "прошлая реплика"


@pytest.mark.asyncio
async def test_history_prev_substitutes_last(tmp_path):
    """_history_prev подставляет последнюю запись и сохраняет draft."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(
        threads_dir=str(tmp_path / "threads")
    ).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task("task-1")
        await pilot.pause()
        scr._history.append("first")
        scr._history.append("second")

        scr.query_one("#cmdinput").text = "draft text"

        scr._history_prev()
        await pilot.pause()

        assert scr.query_one("#cmdinput").text == "second"
        assert scr._history_draft == "draft text"
        assert scr._history_index == 1


@pytest.mark.asyncio
async def test_history_prev_walks_back(tmp_path):
    """Повторный _history_prev идёт дальше назад."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(
        threads_dir=str(tmp_path / "threads")
    ).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task("task-1")
        await pilot.pause()
        scr._history.append("a")
        scr._history.append("b")
        scr._history.append("c")

        scr._history_prev()  # c
        scr._history_prev()  # b
        await pilot.pause()
        assert scr.query_one("#cmdinput").text == "b"
        assert scr._history_index == 1


@pytest.mark.asyncio
async def test_history_next_restores_draft(tmp_path):
    """_history_next за newest возвращает draft."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(
        threads_dir=str(tmp_path / "threads")
    ).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task("task-1")
        await pilot.pause()
        scr._history.append("a")
        scr._history.append("b")

        scr.query_one("#cmdinput").text = "my draft"
        scr._history_prev()  # b
        scr._history_next()  # за пределы → draft
        await pilot.pause()

        assert scr.query_one("#cmdinput").text == "my draft"
        assert scr._history_index is None


@pytest.mark.asyncio
async def test_history_prev_noop_on_empty(tmp_path):
    """_history_prev на пустой истории — no-op."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(
        threads_dir=str(tmp_path / "threads")
    ).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task("task-1")
        await pilot.pause()
        scr.query_one("#cmdinput").text = "hello"
        scr._history_prev()
        await pilot.pause()
        assert scr.query_one("#cmdinput").text == "hello"
        assert scr._history_index is None


@pytest.mark.asyncio
async def test_up_arrow_triggers_history_prev_on_first_line(tmp_path):
    """↑ при курсоре на 1-й строке → history prev."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(
        threads_dir=str(tmp_path / "threads")
    ).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task("task-1")
        await pilot.pause()
        scr._history.append("hello world")

        await pilot.press("up")
        await pilot.pause()
        assert pilot.app.query_one("#cmdinput").text == "hello world"


@pytest.mark.asyncio
async def test_cursor_visual_line_helpers_with_soft_wrap():
    """CommandTextArea знает свою ВИЗУАЛЬНУЮ геометрию (soft-wrap): длинная строка
    переносится → курсор логически в row 0, но визуально может быть на нижней
    строке. first/last_visual_line отражают визуальный ряд, а не логический."""
    pytest.importorskip("textual")
    from gc_tui.widgets.command_input import CommandTextArea
    from textual.app import App, ComposeResult

    class _Host(App):
        def compose(self) -> ComposeResult:
            yield CommandTextArea(id="cmdinput")

    async with _Host().run_test(size=(20, 8)) as pilot:
        ta = pilot.app.query_one(CommandTextArea)
        ta.text = "aaaa bbbb cccc dddd eeee ffff gggg"
        await pilot.pause()
        assert (
            ta.wrapped_document.height > 1
        )  # предусловие: реально перенеслось

        ta.move_cursor((0, 0))
        assert ta.cursor_on_first_visual_line() is True
        assert ta.cursor_on_last_visual_line() is False

        ta.move_cursor((0, len(ta.text)))
        assert ta.cursor_on_first_visual_line() is False
        assert ta.cursor_on_last_visual_line() is True


@pytest.mark.asyncio
async def test_up_on_wrapped_lower_visual_line_does_not_trigger_history(
    tmp_path,
):
    """↑ при курсоре на нижней ВИЗУАЛЬНОЙ строке переноса → курсор вверх, история
    не трогается (раньше логич. row 0 ложно звал history_prev — баг soft-wrap)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.command_input import CommandTextArea

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test(
        size=(24, 12)
    ) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task("task-1")
        await pilot.pause()
        scr._history.append("история")

        inp = scr.query_one("#cmdinput", CommandTextArea)
        inp.focus()
        long_line = "aaaa bbbb cccc dddd eeee ffff gggg hhhh"
        inp.text = long_line
        await pilot.pause()
        assert inp.wrapped_document.height > 1  # перенос случился
        inp.move_cursor((0, len(long_line)))
        assert not inp.cursor_on_first_visual_line()  # есть куда вверх
        await pilot.press("up")
        await pilot.pause()
        assert inp.text == long_line  # история НЕ подменила текст


@pytest.mark.asyncio
async def test_down_on_wrapped_upper_visual_line_moves_cursor_not_history(
    tmp_path,
):
    """↓ при курсоре на верхней визуальной строке переноса → курсор спускается на
    строку ниже (history_next не перехватывает)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.command_input import CommandTextArea

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test(
        size=(24, 12)
    ) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task("task-1")
        await pilot.pause()

        inp = scr.query_one("#cmdinput", CommandTextArea)
        inp.focus()
        long_line = "aaaa bbbb cccc dddd eeee ffff gggg hhhh"
        inp.text = long_line
        await pilot.pause()
        assert inp.wrapped_document.height > 1
        inp.move_cursor((0, 0))
        assert not inp.cursor_on_last_visual_line()  # есть куда вниз
        before_y = inp.wrapped_document.location_to_offset(
            inp.cursor_location
        ).y
        await pilot.press("down")
        await pilot.pause()
        after_y = inp.wrapped_document.location_to_offset(
            inp.cursor_location
        ).y
        assert after_y > before_y  # курсор спустился визуально


@pytest.mark.asyncio
async def test_send_appends_to_history_and_resets_state(tmp_path):
    """user-send append'ит в history + reset navigation state."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(
        threads_dir=str(tmp_path / "threads")
    ).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task("task-1")
        await pilot.pause()
        scr._history.append("old")
        scr._history_index = 0
        scr._history_draft = "draft"

        scr._on_user_text_submitted("new send")
        assert scr._history.size == 2
        assert scr._history_index is None
        assert scr._history_draft is None


@pytest.mark.asyncio
async def test_text_change_resets_navigation_state(tmp_path):
    """Любая правка input → выход из режима навигации."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(
        threads_dir=str(tmp_path / "threads")
    ).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task("task-1")
        await pilot.pause()
        scr._history.append("hello")
        scr._history_prev()
        assert scr._history_index == 0

        scr.query_one("#cmdinput").text = "hello modified"
        if hasattr(scr, "_reset_history_navigation"):
            scr._reset_history_navigation()
        await pilot.pause()
        assert scr._history_index is None
        assert scr._history_draft is None
