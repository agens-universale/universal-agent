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
async def test_long_code_line_is_not_wrapped(tui_app_factory):
    """§3.4: строка обрезается, а не переносится — иначе высота блока непредсказуема."""
    app = tui_app_factory(BlockPanel.for_code(LONG_LINE, "python", event_no=1))
    async with app.run_test(size=(40, 24)):
        body = app.query_one(".block-body", Static)
        assert body.size.height == 1


@pytest.mark.asyncio
async def test_body_has_no_extra_blank_line(tui_app_factory):
    app = tui_app_factory(
        BlockPanel.for_code("a = 1\nb = 2", "python", event_no=1)
    )
    async with app.run_test(size=(80, 24)):
        assert app.query_one(".block-body", Static).size.height == 2
