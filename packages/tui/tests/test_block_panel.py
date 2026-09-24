"""BlockPanel — блок трейса: шапка с кнопкой + селектируемое тело."""
from __future__ import annotations

import pytest

pytest.importorskip("textual")

from textual.app import App, ComposeResult
from textual.widgets import Static

from gc_tui.messages import CopyBlockRequest
from gc_tui.widgets.block_panel import DONE_GLYPH, BlockPanel, copy_glyph

CODE = "def f(x):\n    return x + 1"
HEADERS = ["Стратегия", "Принцип"]
ROWS = [["LRU", "вытесняет давние"]]
ALIGNS = ["left", "left"]


class _Host(App):
    """Хост с прозой вокруг блока — чтобы ловить утечку шапки в выделение."""

    CSS = "Static { height: auto; } BlockPanel { height: auto; }"

    def __init__(self, panel: BlockPanel) -> None:
        super().__init__()
        self._panel = panel
        self.copied: list[str] = []

    def compose(self) -> ComposeResult:
        yield Static("проза сверху")
        yield self._panel
        yield Static("проза снизу")

    def on_copy_block_request(self, event: CopyBlockRequest) -> None:
        self.copied.append(event.payload)


def _code_panel() -> BlockPanel:
    return BlockPanel.for_code(CODE, "python", event_no=7)


@pytest.mark.asyncio
async def test_body_holds_normalized_code():
    app = _Host(_code_panel())
    async with app.run_test(size=(60, 20)):
        assert app.query_one(".block-body", Static).visual.plain == CODE


@pytest.mark.asyncio
async def test_header_shows_language_and_copy_glyph():
    app = _Host(_code_panel())
    async with app.run_test(size=(60, 20)):
        assert app.query_one(".block-label", Static).visual.plain == "python"
        assert app.query_one(".block-copy", Static).visual.plain == copy_glyph()


@pytest.mark.asyncio
async def test_table_body_contains_cells():
    app = _Host(BlockPanel.for_table(HEADERS, ROWS, ALIGNS, event_no=3))
    async with app.run_test(size=(60, 20)):
        plain = app.query_one(".block-body", Static).visual.plain
        assert "Стратегия" in plain and "LRU" in plain


@pytest.mark.asyncio
async def test_select_all_on_panel_yields_body_only():
    """Двойной клик по панели не должен тащить метку языка и глиф кнопки."""
    app = _Host(_code_panel())
    async with app.run_test(size=(60, 20)) as pilot:
        app.query_one(BlockPanel).text_select_all()
        await pilot.pause()
        assert app.screen.get_selected_text() == CODE


@pytest.mark.asyncio
async def test_select_all_on_header_yields_nothing():
    app = _Host(_code_panel())
    async with app.run_test(size=(60, 20)) as pilot:
        app.query_one(".block-header").text_select_all()
        await pilot.pause()
        assert app.screen.get_selected_text() == ""


@pytest.mark.asyncio
async def test_copy_button_posts_request_with_clean_code():
    app = _Host(_code_panel())
    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.click(".block-copy")
        await pilot.pause()
        assert app.copied == [CODE]


@pytest.mark.asyncio
async def test_copy_button_works_for_unrecognized_lang():
    """Неопознанный лексером язык (не None, а строка без лексера — fallback на
    моноцвет) не должен ломать копирование: copy_payload считается из content
    ДО попытки подсветки, независимо от её исхода."""
    app = _Host(BlockPanel.for_code(CODE, "zzz_bogus_lang", event_no=1))
    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.click(".block-copy")
        await pilot.pause()
        assert app.copied == [CODE]


@pytest.mark.asyncio
async def test_copy_button_shows_done_glyph_then_restores():
    app = _Host(_code_panel())
    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.click(".block-copy")
        await pilot.pause()
        assert app.query_one(".block-copy", Static).visual.plain == DONE_GLYPH
        app.query_one(BlockPanel)._restore_copy_glyph()
        await pilot.pause()
        assert app.query_one(".block-copy", Static).visual.plain == copy_glyph()


@pytest.mark.asyncio
async def test_table_copy_payload_is_markdown():
    app = _Host(BlockPanel.for_table(HEADERS, ROWS, ALIGNS, event_no=3))
    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.click(".block-copy")
        await pilot.pause()
        assert app.copied and app.copied[0].startswith("| Стратегия |")


@pytest.mark.asyncio
async def test_double_click_copies_and_selects_body():
    app = _Host(_code_panel())
    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.click(".block-body", times=2)
        await pilot.pause()
        assert app.copied == [CODE]
        assert app.screen.get_selected_text() == CODE


@pytest.mark.asyncio
async def test_table_body_rebuilt_on_resize():
    app = _Host(BlockPanel.for_table(HEADERS, ROWS, ALIGNS, event_no=3))
    async with app.run_test(size=(80, 20)) as pilot:
        wide = app.query_one(".block-body", Static).visual.plain
        await pilot.resize_terminal(28, 20)
        await pilot.pause()
        narrow = app.query_one(".block-body", Static).visual.plain
        assert narrow != wide
        assert narrow.count("\n") > wide.count("\n")


@pytest.mark.asyncio
async def test_label_hidden_when_language_unknown():
    app = _Host(BlockPanel.for_code("capacity: 1024", None, event_no=1))
    async with app.run_test(size=(60, 20)):
        assert app.query_one(".block-label", Static).visual.plain == ""
        assert app.query_one(".block-copy", Static).visual.plain == copy_glyph()


@pytest.mark.asyncio
async def test_label_hidden_when_panel_too_narrow():
    """Находка 6: узкой панели метка скрывается целиком (не обрезается до одной
    буквы), а кнопка ❐ остаётся видимой и прижатой вправо (спека §3.3)."""
    app = _Host(_code_panel())
    async with app.run_test(size=(8, 20)):
        assert app.query_one(".block-label", Static).visual.plain == ""
        assert app.query_one(".block-copy", Static).visual.plain == copy_glyph()


@pytest.mark.asyncio
async def test_focusable_flag_controls_tab_stop():
    focusable = BlockPanel.for_code(CODE, "python", event_no=7, focusable=True)
    app = _Host(focusable)
    async with app.run_test(size=(60, 20)):
        assert focusable.can_focus is True
    plain = _code_panel()
    app2 = _Host(plain)
    async with app2.run_test(size=(60, 20)):
        assert plain.can_focus is False


@pytest.mark.asyncio
async def test_drag_through_block_excludes_header():
    """Главный сценарий задачи: протаскивание мышью из прозы над блоком через
    шапку и тело в прозу под ним не должно захватывать метку языка и кнопку —
    только этот жест (а не text_select_all/двойной клик) реально идёт через
    шапку по координатам, поэтому только он ловит её утечку в выделение."""
    from gc_testkit.tui_support import wait_until

    app = _Host(_code_panel())
    async with app.run_test(size=(60, 20)) as pilot:
        statics = list(app.query(Static))
        top_prose = next(w for w in statics if w.visual.plain == "проза сверху")
        bottom_prose = next(w for w in statics if w.visual.plain == "проза снизу")

        await pilot.mouse_down(top_prose, offset=(0, 0))
        await pilot.hover(".block-header", offset=(2, 0))
        await pilot.hover(".block-body", offset=(2, 0))
        await pilot.hover(bottom_prose, offset=(0, 0))
        await pilot.mouse_up(bottom_prose, offset=(0, 0))
        await wait_until(pilot, lambda: app.screen.get_selected_text() is not None,
                          "drag selection landed")

        selected = app.screen.get_selected_text()
        assert "return x + 1" in selected     # фрагмент кода из тела — есть
        assert "python" not in selected        # метка языка — утекла бы из шапки
        assert copy_glyph() not in selected      # глиф кнопки — утёк бы из шапки


@pytest.mark.asyncio
async def test_ctrl_click_on_copy_button_opens_event_not_copy():
    """Находка 4: Ctrl+клик по кнопке ❐ не копирует, а открывает событие —
    спека §2 обещает «Ctrl+клик — без изменений» с любой точки блока."""
    from textual import events
    from textual.app import App, ComposeResult
    from gc_tui.messages import CopyBlockRequest, OpenEventRequest

    received: list[int] = []
    copied: list[str] = []

    class _App(App):
        def compose(self) -> ComposeResult:
            yield BlockPanel.for_code(CODE, "python", event_no=11)

        def on_open_event_request(self, message: OpenEventRequest) -> None:
            received.append(message.event_no)

        def on_copy_block_request(self, message: CopyBlockRequest) -> None:
            copied.append(message.payload)

    async with _App().run_test(size=(60, 20)) as pilot:
        panel = pilot.app.query_one(BlockPanel)
        copy_btn = panel.query_one(".block-copy")
        click = events.Click(
            widget=copy_btn, x=0, y=0, delta_x=0, delta_y=0,
            button=1, shift=False, meta=False, ctrl=True, chain=1,
        )
        panel.on_click(click)
        await pilot.pause()
        assert received == [11]
        assert copied == []


@pytest.mark.asyncio
async def test_ctrl_click_opens_event_exactly_once():
    """Ctrl+клик по блоку открывает событие РОВНО один раз.

    Живой клик обязателен: Textual зовёт ВСЕ одноимённые `on_click` по MRO, и
    без `prevent_default()` на входе `ClickModelMixin.on_click` отработал бы
    вторым — запрос уходил бы дважды, а экран события вставал бы стопкой.
    Синтетический `panel.on_click(...)` идёт мимо диспетчера и разницы не видит.
    """
    from textual.app import App, ComposeResult

    from gc_tui.messages import OpenEventRequest

    received: list[int] = []

    class _App(App):
        CSS = "BlockPanel { height: auto; }"

        def compose(self) -> ComposeResult:
            yield BlockPanel.for_code(CODE, "python", event_no=11)

        def on_open_event_request(self, message: OpenEventRequest) -> None:
            received.append(message.event_no)

    async with _App().run_test(size=(60, 20)) as pilot:
        await pilot.click(".block-body", control=True)
        await pilot.pause()
        assert received == [11]


@pytest.mark.asyncio
async def test_double_click_focuses_panel_when_focusable():
    """Находка 5: двойной клик по телу ставит фокус на панель, если она
    focusable — иначе `Tab` после клика не продолжал бы с этого события.

    Синтетический `on_click()` (как в test_trace_row.py / test_double_click_open.py)
    идёт мимо screen-level MouseDown-фокуса — при живом клике мышью Textual
    фокусирует панель раньше, чем дойдёт до Click, и разницу не увидеть.
    Здесь фокус может дать только явный self.focus() внутри обработчика."""
    from textual import events

    app = _Host(BlockPanel.for_code(CODE, "python", event_no=7, focusable=True))
    async with app.run_test(size=(60, 20)) as pilot:
        panel = app.query_one(BlockPanel)
        # Textual сам фокусирует единственный focusable-виджет при старте —
        # снять фокус явно, иначе тест не отличит автофокус от self.focus().
        panel.blur()
        await pilot.pause()
        assert not panel.has_focus
        body = app.query_one(".block-body", Static)
        click = events.Click(
            widget=body, x=0, y=1, delta_x=0, delta_y=0,
            button=1, shift=False, meta=False, ctrl=False, chain=2,
        )
        panel.on_click(click)
        await pilot.pause()
        assert panel.has_focus


@pytest.mark.asyncio
async def test_partial_drag_inside_code_body_yields_fragment():
    """Т-A (спека §6.3, главная фича): частичный drag внутри тела кода отдаёт
    ровно протащенный фрагмент, а не блок целиком."""
    from gc_testkit.tui_support import wait_until

    app = _Host(_code_panel())
    async with app.run_test(size=(60, 20)) as pilot:
        body = app.query_one(".block-body", Static)
        # строка 1 (0-based): "    return x + 1" — 'r' в колонке 4, пробел
        # после "return" в колонке 10 (конец выделения включительно).
        await pilot.mouse_down(body, offset=(4, 1))
        await pilot.hover(body, offset=(10, 1))
        await pilot.mouse_up(body, offset=(10, 1))
        await wait_until(pilot, lambda: app.screen.get_selected_text() is not None,
                          "partial drag selection landed")
        selected = app.screen.get_selected_text()
        assert selected == "return "
        assert selected != CODE


@pytest.mark.asyncio
async def test_partial_drag_on_wrapped_line_yields_dragged_part_only():
    """Т-B (спека §6.10): строка шире панели — частичный drag отдаёт ровно
    протянутое, а кнопка и двойной клик — строку целиком.

    Раньше такая строка ОБРЕЗАЛАСЬ, и хвост был недостижим; теперь она
    переносится. Перенос отдан Textual, поэтому настоящих разрывов в тексте
    нет — выделение и копия строки остаются целыми.
    """
    from gc_testkit.tui_support import wait_until

    long_line = ("result = calculate_comprehensive_statistical_significance("
                 "alpha, beta, gamma, delta)")
    app = _Host(BlockPanel.for_code(long_line, "python", event_no=1))
    async with app.run_test(size=(40, 20)) as pilot:
        body = app.query_one(".block-body", Static)
        assert body.size.width < len(long_line)   # строка реально не влезает

        await pilot.mouse_down(body, offset=(0, 0))
        await pilot.hover(body, offset=(body.size.width - 1, 0))
        await pilot.mouse_up(body, offset=(body.size.width - 1, 0))
        await wait_until(pilot, lambda: app.screen.get_selected_text() is not None,
                          "clipped-line drag selection landed")
        partial = app.screen.get_selected_text()
        assert partial != long_line
        assert long_line.startswith(partial)

        await pilot.click(".block-copy")
        await pilot.pause()
        assert app.copied == [long_line]

        app.copied.clear()
        await pilot.click(body, times=2)
        await pilot.pause()
        assert app.copied == [long_line]
        assert app.screen.get_selected_text() == long_line


@pytest.mark.asyncio
async def test_focused_panel_background_is_focus_bg_not_band_bg(tui_app_factory):
    """Т-C (спека §6.11): фон сфокусированной панели — $focus-bg, не фон покоя
    (иначе тихое удаление `BlockPanel:focus` из styles.tcss тест не заметит).

    Фон покоя — $block-bg: блоки ленты переехали на свой токен, тише полосы
    баннеров ($band-bg)."""
    from gc_tui import palette

    panel = BlockPanel.for_code(CODE, "python", event_no=1, focusable=True)
    app = tui_app_factory(panel)
    async with app.run_test(size=(60, 20)) as pilot:
        # Textual сам фокусирует первый focusable-виджет при старте — снять
        # фокус явно, чтобы проверить именно фон НЕ-сфокусированной панели.
        panel.blur()
        await pilot.pause()
        assert panel.styles.background.hex == palette.BLOCK_BG.upper()
        panel.focus()
        await pilot.pause()
        assert panel.styles.background.hex == palette.FOCUS_BG.upper()


@pytest.mark.asyncio
async def test_copy_button_click_does_not_focus_panel():
    """Клик по ❐ копирует, но панель не выделяется.

    Живой клик обязателен: фокус ставит экран на MouseDown, синтетический вызов
    on_click() этого пути не проходит и разницы не увидит."""
    panel = BlockPanel.for_code(CODE, "python", event_no=7, focusable=True)
    app = _Host(panel)
    async with app.run_test(size=(60, 20)) as pilot:
        panel.blur()   # AUTO_FOCUS сам фокусирует единственный focusable-виджет
        await pilot.pause()
        await pilot.click(".block-copy")
        await pilot.pause()
        assert app.copied == [CODE]
        assert not panel.has_focus


@pytest.mark.asyncio
async def test_body_click_focuses_panel():
    """Позитивный контроль: клик по ТЕЛУ панели по-прежнему выделяет её."""
    panel = BlockPanel.for_code(CODE, "python", event_no=7, focusable=True)
    app = _Host(panel)
    async with app.run_test(size=(60, 20)) as pilot:
        panel.blur()
        await pilot.pause()
        await pilot.click(".block-body")
        await pilot.pause()
        assert panel.has_focus


@pytest.mark.asyncio
async def test_mount_then_immediate_remove_does_not_crash():
    """Retry-коллапс submit_solution (main.py) монтирует панель и тут же,
    синхронно, до обработки очереди Mount/Resize, удаляет её. Mount всё равно
    доходит до on_mount → _refresh_body, а compose() своих детей ещё не
    добавил — тело искать негде. Раньше это ронялось NoMatches и убивало
    приложение; теперь _refresh_body тихо выходит, если детей нет."""
    from textual.containers import VerticalScroll

    class _Trace(App):
        def compose(self) -> ComposeResult:
            yield VerticalScroll(id="t")

    app = _Trace()
    async with app.run_test(size=(60, 20)) as pilot:
        trace = app.query_one("#t", VerticalScroll)
        panel = _code_panel()
        trace.mount(panel)
        panel.remove()   # синхронно, до обработки очереди — воспроизводит гонку
        await pilot.pause()
        assert list(app.query(BlockPanel)) == []
