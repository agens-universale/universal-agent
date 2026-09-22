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

_CHROME = 5  # хром футера: padding'и + зазор между колонками (main.py)


def _need(scr) -> int:
    """Ширина, которую правая колонка заявляет по факту композиции."""
    return max(cell_len(line) for line in scr._compose_footer_right().plain.split("\n"))


def _left_rows(scr, total: int, need: int) -> int:
    from gc_tui.services.notices_ui import footer_left_rows, format_notices_badge

    return footer_left_rows(
        scr._footer_left_commands(), format_notices_badge(scr._collect_footer_notices()), width=total - need - _CHROME
    )


@pytest.mark.asyncio
async def test_right_column_leaves_room_for_notices_suffix():
    """Правая колонка + фактическая левая ≤ ширины окна, и левой хватает ОДНОГО
    ряда: суффикс уведомлений стоит рядом с командами, а не выдавлен и не наехал
    на имя модели."""
    from gc_core.startup_notices import Notice
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import TraceScreen

    app = TuiApp(config={"model": "anthropic/claude-opus-5"})
    async with app.run_test(size=(120, 30)) as pilot:
        scr = app.screen_stack[0]
        assert isinstance(scr, TraceScreen)
        # три предупреждения — суффикс «  ·  ⚠3 · F4/notices»
        app.collect_footer_notices = lambda: [Notice("warning", f"c{i}", f"msg{i}") for i in range(3)]
        await pilot.pause()
        need = _need(scr)
        left = cell_len(scr._footer_left_text())
        assert need + left + _CHROME <= 120, (
            f"правая часть {need} + левая {left} > ширины окна 120: правая часть налезет на счётчик уведомлений"
        )
        assert _left_rows(scr, 120, need) == 1, "левую загнали в два ряда там, где места хватало на один"
        # Односторонние assert'ы выше не ловят излишне зажатую правую часть —
        # она тогда режется без нужды; имя модели на этой ширине обязано быть целым.
        assert "anthropic/claude-opus-5" in scr._compose_footer_right().plain


@pytest.mark.asyncio
async def test_right_column_without_notices_is_unchanged():
    """Без уведомлений суффикса нет — та же проверка на голой левой части."""
    from gc_tui.app import TuiApp

    app = TuiApp(config={"model": "m"})
    async with app.run_test(size=(120, 30)) as pilot:
        scr = app.screen_stack[0]
        app.collect_footer_notices = list
        await pilot.pause()
        need = _need(scr)
        assert need + cell_len(scr._footer_left_text()) + _CHROME <= 120
        assert _left_rows(scr, 120, need) == 1


# --- Лесенка жертв: жертва обязана что-то давать, а стоп-клавиша неприкосновенна ---

_PRICING = {"enabled": True, "input": 1.0, "output": 2.0, "currency": "₽", "per_tokens": 1_000_000}


class _RunningRunner:
    """Раннер с ценами; `is_running` — чтобы левая колонка стала «Ctrl+C/stop»."""

    def __init__(self, snapshot, running=False):
        self.snapshot = snapshot
        self._running = running

    def usage_snapshot(self):
        return self.snapshot

    def is_running(self):
        return self._running


def _billed(inp, out, cost):
    """Смешанный счёт: часть провайдер выставил в долларах, часть считается по
    тарифу в рублях — сегмент несёт обе валюты и знак «~». Это и есть строка цен
    боевой ширины (51 колонка против 37 у одновалютной): именно она после правок
    develop стала диктовать ширину правой колонки."""
    from gc_core.usage.account import CATEGORY_MAIN, UsageAccount

    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, inp, out, 0, cost=cost, billed=True, currency="$")
    acc.add("summary", inp // 2, out, 0)
    return acc.to_meta()


def _with_prices(app, scr, *, running=False):
    app._config = {
        "model": "some-long-model-name",
        "pricing": _PRICING,
        "completions_url": "https://routerai.ru/api/v1/chat/completions",
    }
    app.tui_state.active_task_id = "t1"
    scr._active_task_id = "t1"
    app.active_runner = _RunningRunner((_billed(2_000_000, 0, 20.0), _billed(1_000_000, 0, 10.0)), running=running)


@pytest.mark.asyncio
async def test_profile_name_survives_when_dropping_it_frees_nothing(tmp_path, monkeypatch):
    """Цены стоят на второй строке и шире шапки — значит ширину правой колонки
    диктуют ОНИ, и снятие имени профиля не освобождает ни колонки. Раньше лесенка
    всё равно доходила до последней ступени и отдавала имя вместе со стрелкой
    даром. Теперь ужатие шапки перебирается заново на каждом уровне цен, поэтому
    имя возвращается, как только жертва ценами освободила место."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from pathlib import Path as _P

    from gc_core.config_modules.profiles import ProfileRef
    from gc_tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path / "t"), config={"model": "m"})
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(60, 30)) as pilot:
        scr = pilot.app.screen
        monkeypatch.setattr(
            app, "active_profile_ref", lambda: ProfileRef(name="work", path=_P("x"), root_kind="project")
        )
        _with_prices(app, scr)
        await pilot.pause()

        right = scr._compose_footer_right().plain
        assert "work" in right, f"имя профиля снято даром: {right!r}"
        assert "some-long-model-name" in right  # модель не трогаем никогда


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [55, 60, 65, 70, 80])
async def test_stop_key_keeps_its_room_on_narrow_window(tmp_path, monkeypatch, width):
    """Во время хода слева живёт «Ctrl+C/stop» — усечь её нечем, а другого способа
    остановить агента нет. Правая колонка обязана оставить ей место, пожертвовав
    ценами; раньше последняя ступень возвращалась безусловно и выдавливала левую
    в ноль колонок (замер: на 55-60 колонках слева не оставалось ничего)."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import _FOOTER_RUNNING_LEFT

    app = TuiApp(threads_dir=str(tmp_path / "t"), config={"model": "m"})
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(width, 30)) as pilot:
        scr = pilot.app.screen
        _with_prices(app, scr, running=True)
        await pilot.pause()

        assert scr._footer_left_commands() == _FOOTER_RUNNING_LEFT
        left_width = scr._footer_total_width() - _need(scr) - _CHROME
        assert left_width >= cell_len(_FOOTER_RUNNING_LEFT), (
            f"ширина {width}: левой осталось {left_width} колонок, "
            f"а «{_FOOTER_RUNNING_LEFT}» требует {cell_len(_FOOTER_RUNNING_LEFT)}"
        )
