"""Общие хелперы TUI-тестов, импортируемые из тестовых файлов.

Раньше жили прямо в conftest набора TUI, и тесты тянули их импортом из этого
conftest. После разделения наборов по пакетам так больше нельзя: каталогов
``tests`` стало несколько, и общего имени, по которому они видны друг другу,
не осталось. Фикстуры и хуки остались в conftest пакета TUI, а импортируемые
функции переехали сюда.

Здесь только то, что НЕ знает про конкретные экраны и не зависит от файлов набора:
ожидание условия, открытие секции по имени и настройки окна для теста. Монтаж форм, их фейки и изоляторы —
в `packages/tui/tests/form_harness.py`: они лениво импортируют соседние тест-модули
(`test_settings_screen`, `test_settings_voice`, …), и держать их здесь значило бы,
что общий дистрибутив зависит от тестовых файлов одного пакета. Такое было с
вливания develop 26.08.2026 по 27.08.2026.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional


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


def tui_settings(config: Optional[Mapping[str, Any]] = None, **dotted: Any):
    """Настройки окна (`LoadedSettings`) для `TuiApp(settings=...)` в тесте.

    То же, что `gc_testkit.settings.make_settings` (плоские имена и пути через
    точку, строгая валидация), плюс одно правило набора TUI: тест, не задавший
    ``save_logs`` явно, логов агента не пишет — умолчание модели «писать», а
    каталог логов в тестовом конфиге не задан. Тест, которому логи нужны,
    задаёт ``save_logs`` (или ``logging.save_logs``) сам.
    """
    from gc_testkit.settings import make_settings

    values = dict(config or {})
    if not {"save_logs", "logging.save_logs"} & (set(values) | set(dotted)):
        values["save_logs"] = False
    return make_settings(values, **dotted)


def as_settings(value: Any = None):
    """Настройки для экрана из того, что тест держит под рукой: готовые
    (`LoadedSettings` окна, `AppConfig`) — как есть, словарь теста (плоские имена
    и пути через точку) — через `make_settings`, None — умолчания."""
    from gc_core.settings import AppConfig, LoadedSettings

    from gc_testkit.settings import make_settings

    if isinstance(value, LoadedSettings):
        return value
    if isinstance(value, AppConfig):
        return LoadedSettings(settings=value)
    return make_settings(value)


def span_style(text, needle: str) -> str:
    """Стиль спана `rich.Text`, накрывающего первый символ подстроки.

    Возвращает `""`, когда подстрока не покрыта ни одним спаном (цвет виджета).
    Общий для тестов экрана изменений: там счётчики `+N`/`−M` красятся точечно,
    и проверять надо именно спан, а не цвет всего виджета.
    """
    start = text.plain.index(needle)
    for span in text.spans:
        if span.start <= start < span.end:
            return str(span.style)
    return ""
