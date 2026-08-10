"""Правая часть футера не должна отбирать место у левой: при уведомлениях левая
вырастает на суффикс «  ·  ⚠N · F4/notices», и без учёта этого счётчик наезжает
на имя модели (смок 2026-08-02).

Task 17 переставил механику: правая колонка (#footer-right, width:auto)
ЗАЯВЛЯЕТ потребность, левая (1fr) получает остаток — а лесенка
`_compose_footer_right` заявляет ровно столько, чтобы левой хватило её рядов.
Поэтому сторожим не промежуточный «бюджет», а ИТОГ этого решения: фактическую
композицию правой рядом с фактической левой."""
import pytest
from rich.cells import cell_len

_CHROME = 5   # хром футера: padding'и + зазор между колонками (main.py)


def _need(scr) -> int:
    """Ширина, которую правая колонка заявляет по факту композиции."""
    return max(cell_len(line)
               for line in scr._compose_footer_right().plain.split("\n"))


def _left_rows(scr, total: int, need: int) -> int:
    from tui.services.notices_ui import footer_left_rows, format_notices_badge
    return footer_left_rows(scr._footer_left_commands(),
                            format_notices_badge(scr._collect_footer_notices()),
                            width=total - need - _CHROME)


@pytest.mark.asyncio
async def test_right_column_leaves_room_for_notices_suffix():
    """Правая колонка + фактическая левая ≤ ширины окна, и левой хватает ОДНОГО
    ряда: суффикс уведомлений стоит рядом с командами, а не выдавлен и не наехал
    на имя модели."""
    from tui.app import TuiApp
    from tui.screens.main import TraceScreen
    from startup_notices import Notice

    app = TuiApp(config={"model": "anthropic/claude-opus-5"})
    async with app.run_test(size=(120, 30)) as pilot:
        scr = app.screen_stack[0]
        assert isinstance(scr, TraceScreen)
        # три предупреждения — суффикс «  ·  ⚠3 · F4/notices»
        app.collect_footer_notices = lambda: [
            Notice("warning", f"c{i}", f"msg{i}") for i in range(3)
        ]
        await pilot.pause()
        need = _need(scr)
        left = cell_len(scr._footer_left_text())
        assert need + left + _CHROME <= 120, (
            f"правая часть {need} + левая {left} > ширины окна 120: "
            "правая часть налезет на счётчик уведомлений")
        assert _left_rows(scr, 120, need) == 1, (
            "левую загнали в два ряда там, где места хватало на один")
        # Односторонние assert'ы выше не ловят излишне зажатую правую часть —
        # она тогда режется без нужды; имя модели на этой ширине обязано быть целым.
        assert "anthropic/claude-opus-5" in scr._compose_footer_right().plain


@pytest.mark.asyncio
async def test_right_column_without_notices_is_unchanged():
    """Без уведомлений суффикса нет — та же проверка на голой левой части."""
    from tui.app import TuiApp

    app = TuiApp(config={"model": "m"})
    async with app.run_test(size=(120, 30)) as pilot:
        scr = app.screen_stack[0]
        app.collect_footer_notices = lambda: []
        await pilot.pause()
        need = _need(scr)
        assert need + cell_len(scr._footer_left_text()) + _CHROME <= 120
        assert _left_rows(scr, 120, need) == 1
