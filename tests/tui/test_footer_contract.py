"""Футер обещает ровно те клавиши, что работают здесь и сейчас (спека §6).

Сторож ловит два класса лжи разом: обещание УСТАРЕВШЕЙ роли клавиши (Tab как проба,
Ctrl+D как удаление) и МОЛЧАНИЕ о работающей. Второе не мягче первого: после того как
Tab стал переходом между группами, а стрелка из строки режима в поля ходить перестала,
человек, пометивший «Свой endpoint», видел поля и не имел способа узнать, чем в них
войти."""
import pytest

pytest.importorskip("textual")


def footer_wrapped_lines(text: str, cols: int) -> int:
    """Сколько строк займёт футер на окне шириной `cols`.

    Считается переносом по словам — так же, как его рисует Textual. Счётчик символов
    для этого не годится: он не знает, где стоят пробелы, и одинаковой длины строки
    ложатся то в две строки, то в три."""
    import io

    from rich.console import Console
    from rich.text import Text
    inner = cols - 2                       # `#set-footer { padding: 0 1 }`
    return len(Text(text).wrap(Console(width=inner, file=io.StringIO()), inner))


def _all_footer_strings():
    """Все футерные строки ОБОИХ экранов форм, включая лежащие в словаре `_FOOTERS`.

    Перебор по `dir()` + `isinstance(str)` пропустил бы главный футер «Подключения»:
    он живёт значением словаря, а не отдельной константой. Мастер перебирается
    наравне с настройками: сторож, знающий один экран, пропустил бы ту же ложь,
    переехавшую на соседний."""
    from tui.screens import onboarding, settings
    out = {}
    for module in (settings, onboarding):
        for name in dir(module):
            if not name.startswith("_FOOTER"):
                continue
            value = getattr(module, name)
            if isinstance(value, str):
                out[f"{module.__name__}.{name}"] = value
            elif isinstance(value, dict):
                for key, item in value.items():
                    if isinstance(item, str):
                        out[f"{module.__name__}.{name}[{key!r}]"] = item
    return out


def _all_hint_strings():
    """Хинты списков-редакторов (`field_list`/`preset_list`) — тот же словарь клавиш,
    что в футерах экранов, но живёт отдельными константами ВНЕ `settings`/`onboarding`:
    сторож, смотрящий только в экраны (`_all_footer_strings`), пропустил бы подмену
    клавиши здесь (спека 2026-08-06 §4.5, задача 5 бланка).

    Суффикс `_HINT`/`_HINT_NO_MATCH` — не `_HINT_CLASS` (CSS-класс, не текст)."""
    from tui.editors import field_list, preset_list
    out = {}
    for module in (field_list, preset_list):
        for name in dir(module):
            if not (name.endswith("_HINT") or name.endswith("_HINT_NO_MATCH")):
                continue
            value = getattr(module, name)
            if isinstance(value, str):
                out[f"{module.__name__}.{name}"] = value
    return out


def test_no_footer_promises_tab_as_probe():
    """Tab больше не проверяет связь — ни один футер не смеет это обещать."""
    for name, value in _all_footer_strings().items():
        assert "Tab — провер" not in value, f"{name} обещает устаревшую роль Tab"


def test_no_footer_promises_ctrl_d_as_delete():
    """Удаление везде на Del: Ctrl+D как «удалить» в футерах не осталось."""
    for name, value in _all_footer_strings().items():
        assert "Ctrl+D — удалить" not in value, f"{name} обещает устаревшую клавишу"


def test_no_footer_says_bare_update():
    """Два «обновить» разведены (спека §5.4): Ctrl+U меняет систему («обновить
    версию»), Alt+↓ только перечитывает («перечитать список»). Слова «обновить» без
    уточнения в футерах не остаётся."""
    for name, value in _all_footer_strings().items():
        for word in ("обновить", "обновление"):
            assert word not in value or "обновить версию" in value, \
                f"{name}: «{word}» без уточнения — непонятно, что именно обновится"


def test_all_footers_fit_the_two_line_budget():
    """`#set-footer`/`#onb-footer` — те же две строки БЕЗ скролла на ОБОИХ экранах
    (`styles.tcss`: одинаковые `height: 2; padding: 0 1`) — итоговая строка должна
    укладываться в них на самом узком окне (`_FOOTER_MIN_COLS`).

    Раньше сторож смотрел ТОЛЬКО на часть констант: словарь `settings._FOOTERS` и
    футеры мастера оставались непроверенными — и именно там нашёлся реальный дефект
    (задача 5): `_FOOTERS[_CONNECTION]` выросло дописыванием `Alt+↓ — показать список`
    со 108 до 136 символов и на 64 колонках стало рваться на ТРИ строки — хвост
    «Esc — меню» срезался. Общий контейнер — общий бюджет, общая проверка."""
    from tui.screens import settings as s
    for name, value in _all_footer_strings().items():
        lines = footer_wrapped_lines(value, s._FOOTER_MIN_COLS)
        assert lines <= 2, (f"{name}: {lines} строки на {s._FOOTER_MIN_COLS} колонках "
                            f"— хвост будет отрезан: {value}")


@pytest.mark.asyncio
async def test_wizard_footer_names_the_right_key_per_step(tmp_path):
    """Развод клавиш (спека 2026-08-06 §2, §4.2): `Ctrl+Space` осталась только там,
    где есть проба (шаг «Подключение», карта проб §2.5), раскрытие/перечитывание
    списка ушло на `Alt+↓`. Шаг «Подключение» обещает обе — пробу и `Alt+↓ —
    показать список» (список свёрнут); шаг «Модель» — только `Alt+↓ — перечитать
    список», `Ctrl+Space` там больше не обещана; шаг MCP молчит про обе."""
    from tests.tui.conftest import wizard_connection_step
    async with wizard_connection_step(tmp_path) as (pilot, scr):
        await pilot.press("escape")   # свернуть пресет-лист, раскрытый по умолчанию
        await pilot.pause()
        scr._step = 0
        footer = scr._footer_text()
        assert "Ctrl+Space — проверить связь" in footer
        assert "Alt+↓ — показать список" in footer
        scr._step = 1
        footer = scr._footer_text()
        assert "Alt+↓ — перечитать список" in footer
        assert "Ctrl+Space" not in footer
        scr._step = 2
        footer = scr._footer_text()
        assert "Ctrl+Space" not in footer   # на шаге MCP клавиша молчит
        assert "Alt+↓" not in footer


def _footer_widget(scr) -> str:
    """Текст ВИДЖЕТА футера, а не пересчитанный `_footer_text()`.

    Разница не косметическая: футер считается один раз и записывается в виджет, и
    ошибка «посчитали раньше, чем появилось тело» видна только так. Тест на
    `_footer_text()` её не ловит — он считает заново, уже по готовому дереву."""
    from textual.widgets import Static
    return str(scr.query_one("#onb-footer", Static).render())


@pytest.mark.asyncio
async def test_wizard_connection_footer_yields_to_the_open_preset_list(tmp_path):
    """Шаг «Подключение» открывается с РАСКРЫТЫМ пресет-листом: Enter там подставляет
    адрес и возвращается, а Esc сворачивает список. Футер «Enter — далее · Esc — выход»
    врёт на первом же экране мастера."""
    from tests.tui.conftest import wizard_connection_step
    async with wizard_connection_step(tmp_path) as (_pilot, scr):
        from tui.editors.connection import ConnectionEditor
        assert scr.query_one(ConnectionEditor).presets_open
        footer = _footer_widget(scr)
        assert "Enter — подставить" in footer and "Esc — свернуть" in footer
        assert "Enter — далее" not in footer and "Esc — выход" not in footer


@pytest.mark.asyncio
async def test_wizard_connection_footer_when_no_preset_matches(tmp_path):
    """Свой адрес выбивает из списка все строки: подставлять нечего, Enter просто
    сворачивает список и оставляет набранное."""
    from textual.widgets import Input
    from tests.tui.conftest import wizard_connection_step
    async with wizard_connection_step(tmp_path) as (pilot, scr):
        scr.query_one("#conn-url", Input).value = "https://my-own-host.internal/v1"
        await pilot.pause()
        footer = _footer_widget(scr)
        assert "Enter — подставить" not in footer
        assert "Enter — оставить свой адрес" in footer


@pytest.mark.asyncio
async def test_no_live_wizard_footer_tells_an_outdated_story(tmp_path):
    """Текстовые сторожа выше читают КОНСТАНТЫ, а футер мастера собирается по кускам:
    строка «Ctrl+U — обновить» или «Tab — проверить», зашитая прямо в СБОРКУ, прошла бы
    мимо них. Прогоняем те же правила по собранным строкам.

    Чего этот тест НЕ делает: `_step` переставляется без пересборки тела, поэтому
    смонтирован всё время шаг «Подключение». Для остальных шагов ветки, спрашивающие
    виджеты (раскрытый список, пресет-лист), молчат не потому, что состояние
    проверено, а потому что виджетов нет — их строки стережёт перебор констант и
    живые тесты выше. Здесь проверяется ровно словарь безусловных кусков сборки."""
    from tests.tui.conftest import wizard_connection_step
    async with wizard_connection_step(tmp_path) as (pilot, scr):
        seen = []
        for step in (0, 1, 2, 3):
            scr._step = step
            seen.append(scr._footer_text())
        await pilot.press("tab")
        await pilot.pause()
        seen.append(_footer_widget(scr))
        for footer in seen:
            assert "Tab — провер" not in footer
            assert "Ctrl+D — удалить" not in footer
            assert "обновить" not in footer or "обновить версию" in footer


def _set_footer_widget(scr) -> str:
    """Текст ВИДЖЕТА футера `/settings` (тот же смысл, что `_footer_widget` мастера):
    `_render_view` считает футер ДО монтажа тела секции, и ошибку «посчитали раньше,
    чем редактор раскрыл свой список» видно только на виджете."""
    from textual.widgets import Static
    return str(scr.query_one("#set-footer", Static).render())


@pytest.mark.asyncio
async def test_settings_connection_footer_yields_to_the_open_preset_list(tmp_path):
    """Секция «Подключение» открывается с РАСКРЫТЫМ пресет-листом (адрес пуст или
    совпал с пресетом — чистая установка и все, кто выбирал адрес из списка). Enter
    там подставляет провайдера и не сохраняет, Esc сворачивает список — футер
    «Enter — сохранить · Esc — меню» врёт в самом частом состоянии секции."""
    from config_modules.endpoint_presets import DEFAULT_ENDPOINT_PRESETS
    from tests.tui.conftest import settings_connection
    from tui.editors.connection import ConnectionEditor
    async with settings_connection(tmp_path, url=DEFAULT_ENDPOINT_PRESETS[0].url) as (_pilot, scr):
        assert scr.query_one(ConnectionEditor).presets_open
        footer = _set_footer_widget(scr)
        assert "Enter — подставить" in footer and "Esc — свернуть" in footer
        assert "Enter — сохранить" not in footer and "Esc — меню" not in footer


@pytest.mark.asyncio
async def test_settings_connection_footer_when_no_preset_matches(tmp_path):
    """Свой адрес выбивает из списка все строки: подставлять нечего, Enter просто
    сворачивает список и оставляет набранное (те же слова, что у мастера)."""
    from textual.widgets import Input
    from config_modules.endpoint_presets import DEFAULT_ENDPOINT_PRESETS
    from tests.tui.conftest import settings_connection
    async with settings_connection(tmp_path, url=DEFAULT_ENDPOINT_PRESETS[0].url) as (pilot, scr):
        scr.query_one("#conn-url", Input).value = "https://my-own-host.internal/v1"
        await pilot.pause()
        footer = _set_footer_widget(scr)
        assert "Enter — подставить" not in footer
        assert "Enter — оставить свой адрес" in footer


@pytest.mark.asyncio
async def test_settings_connection_footer_returns_to_the_section_after_esc(tmp_path):
    """Esc свернул список — клавиши снова про секцию: Enter сохраняет, Esc уводит
    в меню. Футер, застрявший на обещаниях списка, врал бы теперь в другую сторону."""
    from config_modules.endpoint_presets import DEFAULT_ENDPOINT_PRESETS
    from tests.tui.conftest import settings_connection
    async with settings_connection(tmp_path, url=DEFAULT_ENDPOINT_PRESETS[0].url) as (pilot, scr):
        await pilot.press("escape")
        await pilot.pause()
        footer = _set_footer_widget(scr)
        assert "Enter — сохранить" in footer and "Esc — меню" in footer
        assert "Esc — свернуть" not in footer


def test_settings_preset_footers_stay_on_one_line_at_the_canon_width():
    """`#set-footer` — две строки, но эти два футера видны в САМОМ частом состоянии
    секции (список раскрыт на входе), и хвост, сорвавшийся на вторую строку, читается
    как обрыв. Бюджет — 120 колонок канонного кадра минус `padding: 0 1` (styles.tcss)."""
    from tui.screens.settings import _FOOTER_PRESET_LIST, _FOOTER_PRESET_LIST_NO_MATCH
    for footer in (_FOOTER_PRESET_LIST, _FOOTER_PRESET_LIST_NO_MATCH):
        assert len(footer) <= 118, f"{len(footer)} > 118: {footer}"


def test_settings_model_footer_promises_alt_down_and_stays_quiet_about_ctrl_space():
    """Клавиша перечитывает список моделей и в настройках тоже, а секция о ней молчала:
    ни в футере, ни строкой-подсказкой (у `ModelEditor` списка со своим хинтом нет).
    При лежавшем сервере секция выглядела безнадёжно сломанной. Молчание о работающей
    клавише — та же ложь, что обещание неработающей (докстринг файла).

    Роль ушла с `Ctrl+Space` на `Alt+↓` (спека 2026-08-06 §2, §4.2): пробы в «Модели»
    нет (§5.2 спеки) — обещать `Ctrl+Space` здесь значило бы соврать про несуществующую
    роль."""
    from tui.screens.settings import _FOOTERS, _MODEL
    assert "Alt+↓ — перечитать список" in _FOOTERS[_MODEL]
    assert "Ctrl+Space" not in _FOOTERS[_MODEL]


def test_both_screens_call_ctrl_space_by_the_canon_words():
    """Развод клавиш (спека 2026-08-06 §2) сделал словарь `Ctrl+Space` однословным:
    клавиша больше не раскрывает и не перечитывает список — эта роль ЦЕЛИКОМ ушла на
    `Alt+↓` (§2, §4.1). Единственная оставшаяся формулировка — «проверить связь», и
    только там, где проба вообще есть (карта §2.5). Прежние «перечитать список» /
    `LIST_CLOSED_HINT` в белом списке были бы дырой: сторож пропустил бы текст,
    вернувший клавише снятую роль."""
    allowed = ("Ctrl+Space — проверить связь",)
    hints = {**_all_footer_strings(), **_all_hint_strings()}
    for name, value in hints.items():
        if "Ctrl+Space" not in value:
            continue
        assert any(word in value for word in allowed), \
            f"{name}: Ctrl+Space назван мимо канон-словаря — {value}"


def test_both_screens_call_alt_down_by_the_canon_words():
    """Словарь `Alt+↓` — ровно две формулировки (спека 2026-08-06 §2.1): «показать
    список» для локального источника (пресеты, язык) — показ ничего не грузит; «перечитать
    список» для сетевого (модели) — показ и есть запрос. Третьей формулировки нет: она
    читалась бы как третья, отдельная роль клавиши."""
    allowed = ("Alt+↓ — показать список", "Alt+↓ — перечитать список")
    hints = {**_all_footer_strings(), **_all_hint_strings()}
    for name, value in hints.items():
        if "Alt+↓" not in value:
            continue
        assert any(word in value for word in allowed), \
            f"{name}: Alt+↓ назван мимо канон-словаря — {value}"


def test_ctrl_down_is_never_named_in_texts():
    """`Ctrl+↓` — тихий синоним `Alt+↓` (спека 2026-08-06 §2, §5.5): доходит до Textual
    чистым именем и работает наравне с `Alt+↓`, но в текстах не называется вовсе — два
    обещания на одно действие только шумят. Сторож смотрит в футеры И хинты редакторов
    разом (§4.5): подмена в любом из них — та же дыра."""
    hints = {**_all_footer_strings(), **_all_hint_strings()}
    for name, value in hints.items():
        assert "Ctrl+↓" not in value, f"{name}: Ctrl+↓ назван в тексте — синоним должен молчать"


def test_network_list_statuses_come_from_one_dictionary():
    """Статусы сетевого списка моделей (идёт загрузка / сбой) — ОДНА константа
    (`field_list.LIST_LOADING`/`LIST_FAILED`), а не текст, списанный в `model.py`
    заново: следующая самостоятельная правка экрана не разведёт формулировки снова.

    Сторож сравнивает IDENTITY, а не текст: одинаковый текст двух РАЗНЫХ констант эту
    дыру не закрыл бы. `model.LIST_LOADING is field_list.LIST_LOADING` доказывает,
    что имя в `model.py` — это импорт, а не повторное объявление."""
    from tui.editors import field_list, model
    assert model.LIST_LOADING is field_list.LIST_LOADING
    assert model.LIST_FAILED is field_list.LIST_FAILED
