"""DiffBlock — инлайн-дифф правки в ленте: шапка, свёртка, копирование."""
from __future__ import annotations

import pytest

pytest.importorskip("textual")

from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Static

from gc_core.agent_edits.journal import EditRecord
from gc_tui.messages import CopyBlockRequest, OpenEventRequest
from gc_tui.services.diff_render import copyable_hunks
from gc_tui.widgets.diff_block import DiffBlock

SHORT_HUNKS = (
    "--- \n"
    "+++ \n"
    "@@ -1,3 +1,3 @@\n"
    " def f():\n"
    "-    return 1\n"
    "+    return 2\n"
)
LIMIT = 5


def _long_hunks(count: int = 30) -> str:
    """Длинный дифф со строками РАЗНОЙ длины.

    Разная длина обязательна: движок добивает строки add/del пробелами до общей
    ширины, и на строках одной длины добивать нечего — проверка «в буфер не
    ушли хвостовые пробелы» стала бы вакуумной.
    """
    lines = [f"@@ -1,{count} +1,{count} @@"]
    lines += [f"+line {i}" + "x" * (i % 7) for i in range(count)]
    return "\n".join(lines) + "\n"


def _record(hunks: str, *, path: str = "src/app.py", add: int = 1, del_: int = 1,
            op: str = "edit", flags: tuple[str, ...] = ()) -> EditRecord:
    return EditRecord(path=path, turn_id="t1", ts=0.0, call_id="c1", op=op,
                      hunks=hunks, add=add, del_=del_, before_sha256=None,
                      after_sha256=None, flags=list(flags))


class _Host(App):
    """Хост с прозой вокруг блока — как в тестах BlockPanel."""

    CSS = "Static { height: auto; } DiffBlock { height: auto; }"

    def __init__(self, block: DiffBlock) -> None:
        super().__init__()
        self._block = block
        self.copied: list[str] = []
        self.opened: list[int] = []

    def compose(self) -> ComposeResult:
        yield Static("проза сверху")
        yield self._block
        yield Static("проза снизу")

    def on_copy_block_request(self, event: CopyBlockRequest) -> None:
        self.copied.append(event.payload)

    def on_open_event_request(self, event: OpenEventRequest) -> None:
        self.opened.append(event.event_no)


def _block(record: EditRecord, *, limit: int = LIMIT,
           compact: bool = False) -> DiffBlock:
    return DiffBlock.for_record(record, event_no=7, collapsed_max_lines=limit,
                                compact=compact)


def _body_lines(app: App) -> int:
    return app.query_one(".block-body", Static).visual.plain.count("\n") + 1


@pytest.mark.asyncio
async def test_branch_shows_counters_without_path():
    """Ветка блока — `└ изменено: +N −M`. Имя файла живёт в строке инструмента
    прямо над блоком, дублировать путь незачем."""
    app = _Host(_block(_record(SHORT_HUNKS, add=12, del_=3)))
    async with app.run_test(size=(70, 20)):
        label = app.query_one(".block-label", Static).visual.plain
        assert label == "└ изменено: +12 −3"
        assert "src/app.py" not in label


@pytest.mark.asyncio
async def test_body_is_rich_text():
    app = _Host(_block(_record(SHORT_HUNKS)))
    async with app.run_test(size=(70, 20)):
        body = app.query_one(".block-body", Static)
        assert isinstance(body.content, Text)
        assert "return 2" in body.visual.plain


@pytest.mark.asyncio
async def test_header_and_tail_excluded_from_selection():
    app = _Host(_block(_record(_long_hunks())))
    async with app.run_test(size=(70, 20)):
        for selector in (".block-label", ".block-copy", ".diff-more"):
            widget = app.query_one(selector, Static)
            assert type(widget).ALLOW_SELECT is False
            assert widget.get_selection(None) is None


@pytest.mark.asyncio
async def test_long_diff_collapsed_then_toggled_by_tail_click():
    app = _Host(_block(_record(_long_hunks(30))))
    async with app.run_test(size=(70, 40)) as pilot:
        assert _body_lines(app) == LIMIT
        tail = app.query_one(".diff-more", Static)
        assert tail.visual.plain == f"… ещё {30 - LIMIT} строк"

        await pilot.click(".diff-more")
        await pilot.pause()
        assert _body_lines(app) == 30
        assert app.query_one(".diff-more", Static).visual.plain == "свернуть"

        await pilot.click(".diff-more")
        await pilot.pause()
        assert _body_lines(app) == LIMIT
        assert app.query_one(".diff-more", Static).visual.plain.startswith("… ещё")


@pytest.mark.asyncio
@pytest.mark.parametrize("remaining,label", [
    (1, "1 строка"),
    (2, "2 строки"),
    (5, "5 строк"),
    (21, "21 строка"),
])
async def test_tail_label_plural_forms(remaining, label):
    app = _Host(_block(_record(_long_hunks(LIMIT + remaining))))
    async with app.run_test(size=(70, 40)):
        tail = app.query_one(".diff-more", Static)
        assert tail.visual.plain == f"… ещё {label}"


@pytest.mark.asyncio
async def test_short_diff_has_no_tail():
    app = _Host(_block(_record(SHORT_HUNKS)))
    async with app.run_test(size=(70, 20)):
        assert _body_lines(app) == 3
        assert not app.query(".diff-more")


@pytest.mark.asyncio
async def test_copy_button_posts_raw_hunks():
    app = _Host(_block(_record(_long_hunks())))
    async with app.run_test(size=(70, 40)) as pilot:
        await pilot.click(".block-copy")
        await pilot.pause()
        assert app.copied == [_long_hunks()]
        payload = app.copied[0]
        # Заливка фона добивает строки пробелами до общей ширины, а гаттер
        # печатает номер строки — в буфер уходят сырые хэнки, а не .plain тела.
        body = app.query_one(".block-body", Static).visual.plain
        assert any(line.endswith(" ") for line in body.splitlines())   # не вакуум
        assert not any(line.endswith(" ") for line in payload.splitlines())
        assert "line 0" in payload and "1 +line" not in payload


@pytest.mark.asyncio
async def test_double_click_on_body_copies():
    """Payload — хэнки записи, но с заполненным именем файла (Ж5 батча 4):
    двойной клик копирует то же, что кнопка ❐, — дифф, применимый патчем."""
    app = _Host(_block(_record(SHORT_HUNKS)))
    async with app.run_test(size=(70, 20)) as pilot:
        await pilot.click(".block-body", times=2)
        await pilot.pause()
        assert app.copied == [copyable_hunks(SHORT_HUNKS, path="src/app.py")]


@pytest.mark.asyncio
async def test_double_click_on_compact_block_copies():
    """У компактного блока тела в DOM нет, но копируется он как обычный.

    Payload копирования — сырые хэнки записи, от наличия тела он не зависит:
    кнопка ❐ у компактного блока работала, а двойной клик молчал."""
    app = _Host(_block(_record(_long_hunks(30)), compact=True))
    async with app.run_test(size=(70, 40)) as pilot:
        assert not app.query(".block-body")
        await pilot.click(".block-label", times=2)
        await pilot.pause()
        assert app.copied == [_long_hunks(30)]


@pytest.mark.asyncio
async def test_double_click_on_compact_tail_copies_nothing():
    """Хвост компактного блока по-прежнему только разворачивает."""
    app = _Host(_block(_record(_long_hunks(30)), compact=True))
    async with app.run_test(size=(70, 40)) as pilot:
        await pilot.click(".diff-more", times=2)
        await pilot.pause()
        assert _body_lines(app) == 30
        assert app.copied == []


@pytest.mark.asyncio
async def test_record_without_hunks_shows_branch_only():
    """Бинарь/удаление (`no_diff`): только ветка со счётчиками — ни тела, ни хвоста."""
    record = _record("", path="assets/logo.png", op="binary", add=0, del_=0,
                     flags=("no_diff",))
    app = _Host(_block(record))
    async with app.run_test(size=(70, 20)):
        assert app.query_one(".block-label", Static).visual.plain == "└ изменено: +0 −0"
        assert not app.query(".block-body")
        assert not app.query(".diff-more")


@pytest.mark.asyncio
async def test_compact_block_shows_branch_and_tail_without_body():
    """Компактный блок старого хода: ветка со счётчиками + хвост, тела нет."""
    app = _Host(_block(_record(_long_hunks(30), add=30, del_=0), compact=True))
    async with app.run_test(size=(70, 40)):
        assert app.query_one(".block-label", Static).visual.plain == "└ изменено: +30 −0"
        assert not app.query(".block-body")
        assert app.query_one(".diff-more", Static).visual.plain == "… ещё 30 строк"


@pytest.mark.asyncio
async def test_double_click_on_tail_toggles_once_and_copies_nothing():
    """Textual шлёт по двойному клику chain=1 И chain=2.

    Первый клик разворачивает, второй должен быть съеден веткой хвоста целиком:
    фильтр `chain == 1` на ВХОДЕ в ветку ронял второй клик в ветку копирования
    тела, и клик по хвосту молча клал весь дифф в буфер."""
    app = _Host(_block(_record(_long_hunks(30))))
    async with app.run_test(size=(70, 40)) as pilot:
        assert _body_lines(app) == LIMIT
        await pilot.click(".diff-more", times=2)
        await pilot.pause()
        assert _body_lines(app) == 30
        assert app.copied == []


@pytest.mark.asyncio
async def test_block_is_not_focusable_at_all():
    """Блок не принимает фокус ни от Tab, ни программно (живая правка
    2026-08-11).

    Событие трейса — это ВЫЗОВ ИНСТРУМЕНТА; дифф лишь его часть, поэтому Tab
    обязан идти по событиям, а не останавливаться отдельно на диффе. Следствие:
    Enter-тогла свёртки нет — разворачивает только клик по хвосту."""
    block = _block(_record(_long_hunks(30)))
    app = _Host(block)
    async with app.run_test(size=(70, 40)) as pilot:
        assert block.can_focus is False
        app.set_focus(block)
        await pilot.pause()
        assert not block.has_focus
        await pilot.press("tab")
        await pilot.pause()
        assert not block.has_focus


@pytest.mark.asyncio
async def test_enter_does_not_toggle_anymore():
    """Свёртку переключает только мышь: клавиатурного пути к блоку больше нет."""
    block = _block(_record(_long_hunks(30)))
    app = _Host(block)
    async with app.run_test(size=(70, 40)) as pilot:
        before = _body_lines(app)
        await pilot.press("enter")
        await pilot.pause()
        assert _body_lines(app) == before
        assert app.opened == []


@pytest.mark.asyncio
async def test_ctrl_click_opens_event():
    """Ctrl+клик открывает событие — как у BlockPanel (синтетический клик, как в
    его тесте: живой клик идёт мимо этой ветки через mousedown-фокус)."""
    from textual import events

    block = _block(_record(SHORT_HUNKS))
    app = _Host(block)
    async with app.run_test(size=(70, 20)) as pilot:
        body = app.query_one(".block-body", Static)
        block.on_click(events.Click(
            widget=body, x=0, y=0, delta_x=0, delta_y=0,
            button=1, shift=False, meta=False, ctrl=True, chain=1,
        ))
        await pilot.pause()
        assert app.opened == [7]
        assert app.copied == []


@pytest.mark.asyncio
async def test_copy_button_click_does_not_focus_block():
    """Клик по ❐ копирует, но блок не выделяется.

    Живой клик обязателен: фокус ставит экран на MouseDown, синтетический вызов
    on_click() этого пути не проходит и разницы не увидит."""
    block = _block(_record(_long_hunks(30)))
    app = _Host(block)
    async with app.run_test(size=(70, 40)) as pilot:
        block.blur()   # AUTO_FOCUS сам фокусирует единственный focusable-виджет
        await pilot.pause()
        await pilot.click(".block-copy")
        await pilot.pause()
        assert app.copied == [_long_hunks(30)]
        assert not block.has_focus


@pytest.mark.asyncio
async def test_tail_click_does_not_focus_block():
    """Клик по хвосту переключает свёртку, но блок не выделяется.

    Оба состояния хвоста — это один и тот же виджет `.diff-more`, поэтому
    проверяются оба: и «… ещё N строк», и «свернуть»."""
    block = _block(_record(_long_hunks(30)))
    app = _Host(block)
    async with app.run_test(size=(70, 40)) as pilot:
        block.blur()
        await pilot.pause()
        await pilot.click(".diff-more")          # «… ещё N строк» → развернуть
        await pilot.pause()
        assert _body_lines(app) == 30
        assert not block.has_focus

        await pilot.click(".diff-more")          # «свернуть» → свернуть
        await pilot.pause()
        assert _body_lines(app) == LIMIT
        assert not block.has_focus


@pytest.mark.asyncio
async def test_body_click_does_not_focus_block():
    """Мышь блок НЕ выделяет — ни по кнопке, ни по хвосту, ни по телу.

    Живая правка 2026-08-11: дифф читают, а не «выбирают», и светло-серая
    подсветка на пол-экрана мешала читать код. Клавиатурный путь при этом жив
    (см. соседние тесты Tab/Enter), а выделение текста мышью — тоже (тело
    остаётся селектируемым Static)."""
    block = _block(_record(_long_hunks(30)))
    app = _Host(block)
    async with app.run_test(size=(70, 40)) as pilot:
        block.blur()
        await pilot.pause()
        await pilot.click(".block-body")
        await pilot.pause()
        assert not block.has_focus


@pytest.mark.asyncio
async def test_live_ctrl_click_opens_event_without_focus():
    """Ctrl+клик живой мышью открывает событие и НЕ выделяет блок.

    Выделять нечего: блок не фокусируем вовсе — фокус живёт на строке
    инструмента, к которой дифф относится."""
    block = _block(_record(SHORT_HUNKS))
    app = _Host(block)
    async with app.run_test(size=(70, 20)) as pilot:
        await pilot.click(".block-body", control=True)
        await pilot.pause()
        assert app.opened == [7]
        assert app.copied == []
        assert not block.has_focus


def _counting_render(monkeypatch) -> list:
    """Счётчик вызовов движка: список (max_lines, fill_width) на каждый рендер."""
    import gc_tui.widgets.diff_block as module

    calls: list[tuple] = []
    original = module.render_hunks

    def counting(hunks, *, path, max_lines=None, syntax=True, fill_width=None,
                 **kwargs):
        # `**kwargs` — чтобы заглушка не отставала от сигнатуры (перенос,
        # и что появится дальше): считаем мы вызовы, а не аргументы.
        calls.append((max_lines, fill_width))
        return original(hunks, path=path, max_lines=max_lines, syntax=syntax,
                        fill_width=fill_width, **kwargs)

    monkeypatch.setattr(module, "render_hunks", counting)
    return calls


@pytest.mark.asyncio
async def test_render_cached_per_state(monkeypatch):
    """Повтор состояния на той же ширине движок не зовёт."""
    calls = _counting_render(monkeypatch)

    app = _Host(_block(_record(_long_hunks())))
    async with app.run_test(size=(70, 40)) as pilot:
        # compose (ширины ещё нет) + первый layout — и ни одного лишнего
        assert [s for s, _ in calls] == [LIMIT] * len(calls)
        assert len(calls) <= 2
        await pilot.click(".diff-more")      # развернуть — первый полный рендер
        await pilot.pause()
        assert [s for s, _ in calls][-1] is None
        before = len(calls)
        await pilot.click(".diff-more")      # свернуть — из кэша
        await pilot.pause()
        await pilot.click(".diff-more")      # развернуть — из кэша
        await pilot.pause()
        assert len(calls) == before


@pytest.mark.asyncio
async def test_resize_to_same_width_does_not_rerender(monkeypatch):
    """Ресайз без смены ширины (только высота) движок не дёргает — иначе каждый
    шаг протяжки рамки стоил бы полного рендера (2000 строк — 114 мс)."""
    calls = _counting_render(monkeypatch)

    app = _Host(_block(_record(_long_hunks())))
    async with app.run_test(size=(70, 40)) as pilot:
        before = len(calls)
        await pilot.resize_terminal(70, 30)
        await pilot.pause()
        assert len(calls) == before


@pytest.mark.asyncio
async def test_body_fill_reaches_block_edge_minus_one_column():
    """Заливка добитых строк тянется до правого края подложки минус один символ,
    а не обрывается по самой длинной строке тела."""
    block = _block(_record(SHORT_HUNKS))
    app = _Host(block)
    async with app.run_test(size=(70, 20)) as pilot:
        rows = app.query_one(".block-body", Static).visual.plain.split("\n")
        assert len(rows[1]) == len(rows[2]) == block.size.width - 2
        assert len(rows[0]) < len(rows[1])   # контекст не добивается

        await pilot.resize_terminal(50, 20)
        await pilot.pause()
        rows = app.query_one(".block-body", Static).visual.plain.split("\n")
        assert len(rows[1]) == len(rows[2]) == block.size.width - 2


@pytest.mark.asyncio
async def test_expanded_body_keeps_fill_after_toggle():
    """Развёрнутое тело заливается по той же ширине, что и свёрнутое."""
    block = _block(_record(_long_hunks(30)))
    app = _Host(block)
    async with app.run_test(size=(70, 40)) as pilot:
        await pilot.click(".diff-more")
        await pilot.pause()
        rows = app.query_one(".block-body", Static).visual.plain.split("\n")
        assert len(rows) == 30
        assert {len(row) for row in rows} == {block.size.width - 2}


# --- CSS-контракт (реальные стили приложения) --------------------------------


@pytest.mark.asyncio
async def test_branch_is_outside_block_background(tui_app_factory):
    """Ветка со счётчиками — на фоне ленты; подложка $block-bg только под телом."""
    from gc_tui import palette

    block = _block(_record(_long_hunks(30)))
    app = tui_app_factory(block)
    async with app.run_test(size=(70, 40)) as pilot:
        block.blur()   # AUTO_FOCUS сам фокусирует единственный focusable-виджет
        await pilot.pause()
        assert block.styles.background.a == 0     # сам блок подложки не несёт
        for selector in (".block-body", ".diff-more"):
            slab = app.query_one(selector, Static)
            assert slab.styles.background.hex == palette.BLOCK_BG.upper(), selector


@pytest.mark.asyncio
async def test_focus_never_repaints_slab(tui_app_factory):
    """Подложка блока не меняется от попытки его сфокусировать.

    Блок не фокусируем (`can_focus=False`), поэтому правил `DiffBlock:focus` в
    стилях быть не должно: подсветка фокуса живёт на строке инструмента."""
    from gc_tui import palette

    block = _block(_record(_long_hunks(30)))
    app = tui_app_factory(block)
    async with app.run_test(size=(70, 40)) as pilot:
        block.focus()
        await pilot.pause()
        body = app.query_one(".block-body", Static)
        assert body.styles.background.hex == palette.BLOCK_BG.upper()


@pytest.mark.asyncio
async def test_copy_and_tail_keep_hover_highlight(tui_app_factory):
    """Кнопка ❐ и хвост подсвечиваются под курсором — правила `:hover` пережили
    переезд шапки из подложки в ветку (юзер проверял это специально)."""
    from gc_tui import palette

    block = _block(_record(_long_hunks(30)))
    app = tui_app_factory(block)
    async with app.run_test(size=(70, 40)) as pilot:
        for selector in (".block-copy", ".diff-more"):
            await pilot.hover(selector)
            await pilot.pause()
            assert app.query_one(selector, Static).styles.color.hex == \
                palette.ACCENT.upper(), selector


@pytest.mark.asyncio
async def test_block_has_blank_line_after_it(tui_app_factory):
    """Следующий инструмент не липнет к блоку: пустая строка снизу."""
    block = _block(_record(SHORT_HUNKS))
    app = tui_app_factory(block)
    async with app.run_test(size=(70, 20)):
        assert block.styles.margin.top == 0      # к своей строке — вплотную
        assert block.styles.margin.bottom == 1


@pytest.mark.asyncio
async def test_body_indented_two_columns_from_branch(tui_app_factory):
    """Лесенка внутри блока: тело диффа на +2 правее ветки."""
    block = _block(_record(_long_hunks(30)))
    app = tui_app_factory(block)
    async with app.run_test(size=(70, 40)):
        branch = app.query_one(".block-label", Static)
        for selector in (".block-body", ".diff-more"):
            assert app.query_one(selector, Static).region.x - branch.region.x == 2, \
                selector
