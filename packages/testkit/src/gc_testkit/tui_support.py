"""Хелперы TUI-тестов, импортируемые из тестовых файлов.

Раньше жили прямо в conftest набора TUI, и тесты тянули их импортом из этого
conftest. После разделения наборов по пакетам так больше нельзя: каталогов
``tests`` стало несколько, и общего имени, по которому они видны друг другу,
не осталось. Фикстуры и хуки остались в conftest пакета TUI, а импортируемые
функции переехали сюда.
"""

from __future__ import annotations

from typing import Callable


async def wait_until(
    pilot,
    cond: Callable[[], bool],
    what: str,
    *,
    steps: int = 200,
    step_s: float = 0.05,
) -> None:
    """Ждать наблюдаемое условие (до ~10 с при дефолтах), не слепой pause."""
    for _ in range(steps):
        if cond():
            return
        await pilot.pause(step_s)
    raise AssertionError(f"timeout: {what}")


def settings_section_index(name: str) -> int:
    """Индекс секции SettingsScreen по отображаемому имени (SECTIONS), а не
    хардкод-номером: вставка новой секции в SECTIONS сдвигает номера соседей,
    но не имена — тест, прибитый к номеру, тихо открывает не ту секцию."""
    from gc_tui.screens.settings import SECTIONS

    return SECTIONS.index(name)


def open_settings_section(scr, name: str) -> None:
    """Открыть секцию SettingsScreen хаба настроек ПО ИМЕНИ вместо хардкод-индекса
    (см. settings_section_index) — драйвер для scr._menu_idx/_open_section."""
    idx = settings_section_index(name)
    scr._menu_idx = idx
    scr._open_section(idx)
