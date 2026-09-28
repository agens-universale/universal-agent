"""CSS-контракт блоков трейса: отступы, отсутствие переноса, подсветка фокуса."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from gc_tui.widgets.block_panel import BlockPanel
from textual.widgets import Static

LONG_LINE = "result = calculate_comprehensive_statistical_significance(alpha, beta, gamma, delta)"


@pytest.mark.asyncio
async def test_code_panel_keeps_top_margin_with_indent(tui_app_factory):
    """Гвоздь на класс ошибок Textual: правило с margin-left затирало весь box."""
    app = tui_app_factory(
        BlockPanel.for_code("x = 1", "python", event_no=1, indent=True)
    )
    async with app.run_test(size=(80, 24)):
        assert app.query_one(BlockPanel).styles.margin.top == 1


@pytest.mark.asyncio
async def test_header_separated_from_body_by_blank_line(tui_app_factory):
    """Шапка не липнет к содержимому: между ними строка воздуха (живая правка 2026-08-04)."""
    app = tui_app_factory(
        BlockPanel.for_code("x = 1\ny = 2", "python", event_no=1)
    )
    async with app.run_test(size=(50, 12)):
        header = app.query_one(".block-header")
        body = app.query_one(".block-body")
        assert body.region.y - (header.region.y + header.region.height) == 1


@pytest.mark.asyncio
async def test_header_has_no_gap_without_language_label(tui_app_factory):
    """Д9: без метки языка воздух под шапкой убирается — код сразу под строкой
    с ⧉ (с меткой поведение прежнее, см. test_header_separated_from_body_by_blank_line)."""
    app = tui_app_factory(BlockPanel.for_code("x = 1\ny = 2", None, event_no=1))
    async with app.run_test(size=(50, 12)):
        header = app.query_one(".block-header")
        body = app.query_one(".block-body")
        assert body.region.y - (header.region.y + header.region.height) == 0


@pytest.mark.asyncio
async def test_copy_button_not_glued_to_panel_edge(tui_app_factory):
    """Кнопка отстоит от правого края панели: свой padding + padding панели."""
    app = tui_app_factory(BlockPanel.for_code("x = 1", "python", event_no=1))
    async with app.run_test(size=(50, 12)):
        panel = app.query_one(BlockPanel)
        button = app.query_one(".block-copy")
        assert panel.region.right - button.region.right >= 1
        assert button.styles.padding.right == 1


@pytest.mark.asyncio
async def test_table_panel_keeps_both_margins_with_indent(tui_app_factory):
    app = tui_app_factory(
        BlockPanel.for_table(["a"], [["b"]], ["left"], event_no=1, indent=True)
    )
    async with app.run_test(size=(80, 24)):
        margin = app.query_one(BlockPanel).styles.margin
        assert (margin.top, margin.bottom) == (1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("indent", [False, True])
async def test_code_panel_gets_bottom_margin(tui_app_factory, indent):
    """Д9: код-панель получает нижний отступ (раньше — только верхний, из-за
    чего следующий за кодом текст той же реплики лип к нему вплотную)."""
    app = tui_app_factory(
        BlockPanel.for_code("x = 1", "python", event_no=1, indent=indent))
    async with app.run_test(size=(80, 24)):
        margin = app.query_one(BlockPanel).styles.margin
        assert (margin.top, margin.bottom) == (1, 1)


@pytest.mark.asyncio
async def test_long_code_line_wraps_instead_of_being_cut(tui_app_factory):
    """Длинная строка кода переносится: хвост не пропадает.

    Прежнее правило — «обрезается, а не переносится, иначе высота блока
    непредсказуема» — снято сознательно. Довод оказался не универсальным: в
    ленте уже есть блок с width-зависимой высотой (таблица пересобирается под
    ширину на каждом resize), а горизонтальной прокрутки в ленте нет вовсе, и
    обрезанный хвост был недостижим ничем.

    Перенос отдан Textual, а не сделан вручную: свой рез вставил бы в текст
    настоящие разрывы, и выделение мышью отдавало бы строку разорванной.
    """
    app = tui_app_factory(BlockPanel.for_code(LONG_LINE, "python", event_no=1))
    async with app.run_test(size=(40, 24)):
        body = app.query_one(".block-body", Static)
        assert body.size.height > 1
        assert len(LONG_LINE) > body.size.width      # строка правда не влезала


@pytest.mark.asyncio
async def test_body_has_no_extra_blank_line(tui_app_factory):
    app = tui_app_factory(
        BlockPanel.for_code("a = 1\nb = 2", "python", event_no=1)
    )
    async with app.run_test(size=(80, 24)):
        assert app.query_one(".block-body", Static).size.height == 2


@pytest.mark.asyncio
async def test_two_code_blocks_in_a_row_share_a_single_blank_line(tui_app_factory):
    """Д9: два код-блока подряд в одной реплике разделены одной пустой строкой,
    не двумя — Textual схлопывает соседние margin как CSS (эмпирика: пробный
    прогон region.y на двух Static с margin 1 0 1 0 дал зазор 1, не 2)."""
    from textual.containers import Vertical

    from gc_tui.services.trace_render import build_agent_widgets

    md = "```python\na = 1\n```\n\n```python\nb = 2\n```"
    app = tui_app_factory(Vertical(*build_agent_widgets(md, event_no=1)))
    async with app.run_test(size=(80, 24)):
        panels = list(app.query(BlockPanel))
        assert len(panels) == 2
        gap = panels[1].region.y - (panels[0].region.y + panels[0].region.height)
        assert gap == 1


@pytest.mark.asyncio
async def test_code_block_then_table_share_a_single_blank_line(tui_app_factory):
    """Тот же схлопывающийся зазор — для соседства код-блока и таблицы (оба
    используют одинаковый принцип нижнего/верхнего отступа 1)."""
    from textual.containers import Vertical

    from gc_tui.services.trace_render import build_agent_widgets

    md = "```python\nx = 1\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    app = tui_app_factory(Vertical(*build_agent_widgets(md, event_no=1)))
    async with app.run_test(size=(80, 24)):
        panels = list(app.query(BlockPanel))
        assert len(panels) == 2
        gap = panels[1].region.y - (panels[0].region.y + panels[0].region.height)
        assert gap == 1


@pytest.mark.asyncio
async def test_code_block_bottom_margin_separates_it_from_next_prose(tui_app_factory):
    """Д9 («нет нижнего отступа»): код-блок и продолжение прозы той же реплики
    (continuation, свой margin-top: 0) — раньше без нижнего отступа код-панели
    текст лип к телу кода вплотную (зазор 0)."""
    from textual.containers import Vertical

    from gc_tui.services.trace_render import build_agent_widgets
    from gc_tui.widgets.trace_row import TraceRow

    md = "вступление\n\n```python\nx = 1\n```\n\nитог"
    app = tui_app_factory(Vertical(*build_agent_widgets(md, event_no=1)))
    async with app.run_test(size=(80, 24)):
        panel = app.query_one(BlockPanel)
        cont = next(w for w in app.query(TraceRow) if w.has_class("line-agent-cont"))
        gap = cont.region.y - (panel.region.y + panel.region.height)
        assert gap == 1
