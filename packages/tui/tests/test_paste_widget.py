"""Pilot-тесты: сворачивание большой вставки в плашку, разворот, отправка."""
import pytest

BIG6 = "line\n" * 5         # 6 строк → сворачивается
BIG5 = "a\n" * 4 + "a"      # 5 строк → сворачивается
LABEL5 = "[Вставка #1 · 5 строк]"


async def _input(pilot):
    from gc_tui.widgets.command_input import CommandTextArea
    return pilot.app.screen.query_one("#cmdinput", CommandTextArea)


@pytest.mark.asyncio
async def test_large_paste_becomes_chip():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    async with TuiApp().run_test() as pilot:
        inp = await _input(pilot)
        inp._handle_paste(BIG6)
        await pilot.pause()
        assert inp.text == "[Вставка #1 · 6 строк]"
        assert inp._chips.get(1) == BIG6


@pytest.mark.asyncio
async def test_small_paste_inserted_as_text():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    async with TuiApp().run_test() as pilot:
        inp = await _input(pilot)
        inp._handle_paste("short")
        await pilot.pause()
        assert inp.text == "short"
        assert inp._chips.get(1) is None


@pytest.mark.asyncio
async def test_second_same_paste_expands_inline():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    async with TuiApp().run_test() as pilot:
        inp = await _input(pilot)
        inp._handle_paste(BIG5)
        await pilot.pause()
        assert inp.text == LABEL5
        inp._handle_paste(BIG5)          # та же паста в окне → разворот
        await pilot.pause()
        assert inp.text == BIG5
        assert inp._chips.get(1) is None


@pytest.mark.asyncio
async def test_different_paste_in_window_makes_new_chip():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    async with TuiApp().run_test() as pilot:
        inp = await _input(pilot)
        inp._handle_paste(BIG5)
        await pilot.pause()
        inp._handle_paste(BIG6)          # другое содержимое → новая плашка
        await pilot.pause()
        assert "[Вставка #1 · 5 строк]" in inp.text
        assert "[Вставка #2 · 6 строк]" in inp.text
        assert inp._chips.get(1) == BIG5 and inp._chips.get(2) == BIG6


@pytest.mark.asyncio
async def test_submission_text_concatenates_in_order():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    async with TuiApp().run_test() as pilot:
        inp = await _input(pilot)
        inp._handle_paste(BIG5)          # плашка #1 в начале
        await pilot.pause()
        inp.insert(" mid ")              # обычный ввод после плашки
        inp._handle_paste("z" * 500)     # плашка #2 (одна длинная строка)
        await pilot.pause()
        assert inp.submission_text() == BIG5 + " mid " + "z" * 500


@pytest.mark.asyncio
async def test_enter_resets_chip_state(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    class _Fake:                                  # no-op executor: Enter не должен
        def run(self, messages, *, thread_id, **kw):  # строить реальный AgentExecutor
            pass

    app = TuiApp(threads_dir=str(tmp_path))
    app._executor_factory = lambda: _Fake()
    async with app.run_test() as pilot:
        inp = await _input(pilot)
        inp._handle_paste(BIG5)
        await pilot.pause()
        await pilot.press("enter")                # submit → виджет чистит стор плашек
        await pilot.pause()
        assert inp.text == ""
        assert inp._chips.get(1) is None
        assert inp._chips.add("x")[0] == 1        # счётчик сброшен


@pytest.mark.asyncio
async def test_bracketed_paste_collapses_without_raw_duplicate():
    """events.Paste (нативная вставка / Ctrl+V в терминалах с bracketed-paste):
    сворачивается в плашку и НЕ дублируется сырым текстом. Регресс: _on_paste без
    prevent_default → базовый TextArea._on_paste тоже вставлял event.text."""
    pytest.importorskip("textual")
    from textual import events
    from gc_tui.app import TuiApp
    async with TuiApp().run_test() as pilot:
        inp = await _input(pilot)
        inp.post_message(events.Paste(BIG5))
        await pilot.pause()
        await pilot.pause()
        assert inp.text == LABEL5            # только плашка, без сырого хвоста
        assert inp._chips.get(1) == BIG5


@pytest.mark.asyncio
async def test_bracketed_paste_small_inserted_once():
    """Маленькая bracketed-вставка попадает в буфер РОВНО один раз (не дублируется)."""
    pytest.importorskip("textual")
    from textual import events
    from gc_tui.app import TuiApp
    async with TuiApp().run_test() as pilot:
        inp = await _input(pilot)
        inp.post_message(events.Paste("short"))
        await pilot.pause()
        await pilot.pause()
        assert inp.text == "short"           # один раз, не "shortshort"


@pytest.mark.asyncio
async def test_cr_separated_paste_collapses_like_lf():
    """Bracketed-paste (Windows Terminal Ctrl+V) разделяет строки символом \\r.
    should_collapse считает \\n — без нормализации многострочная \\r-вставка <500
    симв не сворачивалась (уходила инлайн). _handle_paste нормализует \\r→\\n."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    async with TuiApp().run_test() as pilot:
        inp = await _input(pilot)
        inp._handle_paste("a\rb\rc\rd\re")   # 5 строк через \r, <500 симв
        await pilot.pause()
        assert inp.text == "[Вставка #1 · 5 строк]"   # свернулось по «строкам»
        assert inp._chips.get(1) == "a\nb\nc\nd\ne"   # хранится с нормализованными \n


@pytest.mark.asyncio
async def test_crlf_paste_normalized_inline():
    """Маленькая \\r\\n-вставка (read_clipboard на Windows) — инлайн без мусорных \\r."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    async with TuiApp().run_test() as pilot:
        inp = await _input(pilot)
        inp._handle_paste("a\r\nb")
        await pilot.pause()
        assert inp.text == "a\nb"            # \r\n → \n, без \r


@pytest.mark.asyncio
async def test_expand_works_always_no_timer():
    """Разворот по повторной вставке последней пасты — без лимита времени: после
    сворачивания _expand_chip_id держится, авто-таймера закрытия нет."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    async with TuiApp().run_test() as pilot:
        inp = await _input(pilot)
        inp._handle_paste(BIG5)
        await pilot.pause()
        assert inp._expand_chip_id == 1                      # окно «развернуть» открыто
        assert getattr(inp, "_expand_timer", None) is None   # без авто-закрытия по таймеру
        inp._handle_paste(BIG5)                              # повторная вставка → разворот
        await pilot.pause()
        assert inp.text == BIG5
        assert inp._chips.get(1) is None


@pytest.mark.asyncio
async def test_paste_after_chip_cleared_from_buffer():
    """Если плашку стёрли из буфера (Esc-clear / text=\"\"), повторный Ctrl+V тем же
    текстом не должен молча no-op'ить в ветке «развернуть» — снова плашка."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    async with TuiApp().run_test() as pilot:
        inp = await _input(pilot)
        inp._handle_paste(BIG5)
        await pilot.pause()
        assert inp._expand_chip_id == 1
        inp.text = ""                                         # как Esc-clear: без _clear_chip_state
        await pilot.pause()
        inp._handle_paste(BIG5)
        await pilot.pause()
        assert inp.text.startswith("[Вставка #")
        assert "5 строк]" in inp.text
        assert BIG5 in inp._chips._chips.values()


@pytest.mark.asyncio
async def test_ctrl_v_pastes_when_focus_on_trace(tmp_path, monkeypatch):
    """Кликнули трейс во время run → фокус не в инпуте; Ctrl+V всё равно вставляет."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.command_input import CommandTextArea
    from gc_tui.widgets.trace_row import TraceRow
    from gc_tui.services import os_ops as os_ops

    monkeypatch.setattr(os_ops, "read_clipboard", lambda: "из трейса")

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        row = TraceRow(prefix=">", content="строка", kind_class="line-agent", event_no=1)
        scr.query_one("#trace").mount(row)
        await pilot.pause()
        row.focus()
        await pilot.pause()
        assert not scr.query_one("#cmdinput", CommandTextArea).has_focus
        await pilot.press("ctrl+v")
        await pilot.pause()
        inp = scr.query_one("#cmdinput", CommandTextArea)
        assert inp.text == "из трейса"
        assert inp.has_focus
