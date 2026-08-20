"""Правая часть футера не должна отбирать место у левой: при уведомлениях левая
вырастает на суффикс «  ·  ⚠N · F4/notices», и без учёта этого счётчик наезжает
на имя модели (смок 2026-08-02)."""
import pytest
from rich.cells import cell_len


@pytest.mark.asyncio
async def test_right_budget_accounts_for_notices_suffix():
    """Бюджет правой части + фактическая левая ≤ ширина окна."""
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import TraceScreen
    from gc_core.startup_notices import Notice

    app = TuiApp(config={"model": "anthropic/claude-opus-5"})
    async with app.run_test(size=(120, 30)) as pilot:
        scr = app.screen_stack[0]
        assert isinstance(scr, TraceScreen)
        # три предупреждения — суффикс «  ·  ⚠3 · F4/notices»
        app.collect_footer_notices = lambda: [
            Notice("warning", f"c{i}", f"msg{i}") for i in range(3)
        ]
        await pilot.pause()
        left = cell_len(scr._footer_left_text())
        budget = scr._footer_right_available_width()
        assert left + budget <= 120, (
            f"левая часть {left} + бюджет правой {budget} > ширины окна 120: "
            "правая часть налезет на счётчик уведомлений")
        # Односторонний assert выше не ловит и излишне зажатый бюджет (вычитаемый
        # запас раздут вместо небольшого хрома футера) — правая часть тогда режется
        # без нужды. Допуск (6) шире фактического запаса (5, main.py: хром 3 + зазор
        # между колонками 2), чтобы не
        # спотыкаться о его точное значение, но много меньше явной порчи бюджета.
        assert left + budget >= 120 - 6, (
            f"левая часть {left} + бюджет правой {budget} < 120 − 6: "
            "бюджет правой части занижен сильнее, чем нужно для хрома футера")


@pytest.mark.asyncio
async def test_right_budget_without_notices_is_unchanged():
    """Без уведомлений суффикса нет — бюджет считается как раньше."""
    from gc_tui.app import TuiApp

    app = TuiApp(config={"model": "m"})
    async with app.run_test(size=(120, 30)) as pilot:
        scr = app.screen_stack[0]
        app.collect_footer_notices = lambda: []
        await pilot.pause()
        left = cell_len(scr._footer_left_text())
        budget = scr._footer_right_available_width()
        assert left + budget <= 120
        assert left + budget >= 120 - 6   # бюджет не занижен сильнее хрома футера
