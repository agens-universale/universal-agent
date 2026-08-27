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
    переехавшую на соседний.

    Футеры секций, вынесенных в свои модули-редакторы, перебираются здесь же
    (Global Constraints волны B): у секции «Модели» футер позиционный и живёт
    целиком в `models_section` — сторож, смотрящий только в экраны, не увидел бы
    ни одной её строки."""
    from tui.editors import models_section
    from tui.screens import onboarding, settings
    out = {}
    for module in (settings, onboarding, models_section):
        for name in dir(module):
            if not name.startswith(("_FOOTER", "_VOICE_FOOTER")):
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
    """Хинты списков-редакторов (`field_list`/`preset_list`/`voice_endpoint`) — тот же
    словарь клавиш, что в футерах экранов, но живёт отдельными константами ВНЕ
    `settings`/`onboarding`: сторож, смотрящий только в экраны (`_all_footer_strings`),
    пропустил бы подмену клавиши здесь (спека 2026-08-06 §4.5, задача 5 бланка).

    Суффикс `_HINT`/`_HINT_NO_MATCH` — не `_HINT_CLASS` (CSS-класс, не текст).

    Новый модуль формы регистрируется здесь той задачей, которая его создаёт
    (Global Constraints волны B): `model_entry_form` называет обе клавиши своих
    списков, `models_section` — подсказки строк и своего инлайн-пикера,
    `voice_endpoint` — списки адреса, модели и языка голосовой секции."""
    from tui.editors import (field_list, model_entry_form, models_section,
                             preset_list, voice_endpoint)
    out = {}
    for module in (field_list, model_entry_form, models_section, preset_list,
                   voice_endpoint):
        for name in dir(module):
            if not (name.endswith("_HINT") or name.endswith("_HINT_NO_MATCH")):
                continue
            value = getattr(module, name)
            if isinstance(value, str):
                out[f"{module.__name__}.{name}"] = value
    return out


def _all_notice_strings():
    """Notice- и confirm-тексты редакторов — те же обещания клавиш, что в футерах,
    только сказанные телом секции (спека эффективного вида §5, [Р2a-N15]).

    Отдельный сборщик, а не расширение `_all_footer_strings`: бюджет двух строк —
    про полосу футера, а вопрос-баннер живёт в теле и переносится законно. По ним
    стоит проверять словарь клавиш и грамматику отказов.

    Заодно это сторож «текст — module-level константа»: сообщение, собранное
    f-строкой по месту, в перебор не попадёт вовсе, и подмена клавиши в нём
    осталась бы незамеченной."""
    from tui.editors import model_entry_form, models_section
    from tui.screens import onboarding, settings
    out = {}
    for module in (settings, onboarding, models_section, model_entry_form):
        for name in dir(module):
            if not (name.startswith("_NOTICE") or name.startswith("_CONFIRM")):
                continue
            value = getattr(module, name)
            if isinstance(value, str):
                out[f"{module.__name__}.{name}"] = value
    return out


def test_notice_and_confirm_texts_are_collected_at_all():
    """Сам сборщик — тоже предмет: пустой перебор молча проходил бы все проверки
    ниже. Секция «Модели» несёт ОБА отказа эффективного вида — Del и Enter на
    записи основного конфига: имя, не попавшее в перебор, оставило бы предмет
    стража за его же границей."""
    from tui.editors import models_section as m
    collected = set(_all_notice_strings().values())
    assert m._NOTICE_REMOVE_REFUSED_INHERITED in collected
    assert m._NOTICE_EDIT_REFUSED_INHERITED in collected
    # Итог записи — тоже константа: собранный f-строкой по месту, он в перебор не
    # попадал бы вовсе.
    assert m._NOTICE_ENTRY_SAVED in collected


def test_collected_refusals_follow_the_canon_grammar():
    """Канон отказов Global Constraints — «✗ не <глагол>: <причина>»: одна форма на
    все витрины, иначе отказ читается как сбой, а не как ответ."""
    import re
    for name, value in _all_notice_strings().items():
        if not value.startswith("✗"):
            continue
        assert re.match(r"^✗ не \w+: ", value), f"{name}: отказ мимо канона — {value}"


def test_the_inherited_row_footer_promises_neither_del_nor_enter():
    """На унаследованной строке не работают ни Del, ни Enter — править и удалять
    запись основного конфига секция профиля не вправе. Футер обязан молчать про
    обе клавиши (канон «недоступное не обещаем») и обещать то, что там реально
    работает."""
    from tui.editors import models_section as m
    footer = m._FOOTER_PROFILE_BASE_ENTRY
    assert "Del" not in footer and "Enter" not in footer
    assert "n — добавить" in footer
    assert m._FOOTERS_WITH_VIEW[footer].count("Enter") == 0


def test_the_file_viewer_key_is_promised_only_in_the_profile_sub_view():
    """§11a: `v` открывает файл профиля, и клавиша эта — ПОД-ВИДА профиля.
    Строки её обещания живут производными от футеров под-вида (`_FOOTERS_WITH_
    VIEW`), а не вторыми литералами: дописанная в одном месте клавиша обязана
    появиться в обоих, а бюджет двух строк — считаться и для варианта с `v`
    (он длиннее оригинала, и именно он попадает на экран).

    Ведёт клавишу ЭКРАН (`settings`), поэтому и обещание гасится его настройкой
    — но слова обещания стоят рядом с остальными футерами секции, иначе их не
    видел бы ни один сборщик."""
    from tui.editors import models_section as m
    promised = set(m._FOOTERS_WITH_VIEW.values())
    for name, value in _all_footer_strings().items():
        if m._VIEW_KEY in value:
            assert value in promised, f"{name}: обещает просмотр файла вне под-вида"
    for base, viewed in m._FOOTERS_WITH_VIEW.items():
        assert m._VIEW_KEY not in base
        assert viewed.count(m._VIEW_KEY) == 1
        assert viewed.endswith(m._EXIT_TO_LIST), viewed   # выход остаётся последним


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


def test_settings_voice_footers_name_tab_by_the_group():
    """Глагол Tab зависит от группы под курсором: из строк режима — «в поля», из
    полей — обратно «к режиму». Там, где второй группы нет, Tab не обещан вовсе."""
    from tui.screens import settings as s
    for footer in (s._VOICE_FOOTER_LOCAL, s._VOICE_FOOTER_CLOUD):
        assert s._TAB_TO_FIELDS in footer, f"курсор в вариантах, а «в поля» нет: {footer}"
    for footer in (s._VOICE_FOOTER_LOCAL_LANG, s._VOICE_FOOTER_CLOUD_FIELD,
                   s._VOICE_FOOTER_CLOUD_LIST):
        assert s._TAB_TO_VARIANTS in footer, f"курсор в полях, а «к режиму» нет: {footer}"
    # Сервис не поставлен — полей нет вовсе, переходить некуда (§6.1).
    assert "Tab" not in s._VOICE_FOOTER_LOCAL_FRESH


def test_the_wizard_and_the_settings_speak_of_tab_the_same_way():
    """Один язык форм — один словарь: разъехавшиеся формулировки Tab на двух экранах
    читались бы как две разные клавиши."""
    from tui.screens import onboarding, settings
    assert onboarding._TAB_TO_FIELDS == settings._TAB_TO_FIELDS
    assert onboarding._TAB_TO_VARIANTS == settings._TAB_TO_VARIANTS


def test_all_footers_fit_the_two_line_budget():
    """`#set-footer`/`#onb-footer` — те же две строки БЕЗ скролла на ОБОИХ экранах
    (`styles.tcss`: одинаковые `height: 2; padding: 0 1`) — итоговая строка должна
    укладываться в них на самом узком окне (`_FOOTER_MIN_COLS`).

    Раньше сторож смотрел ТОЛЬКО на `_VOICE_FOOTER_*`: словарь `settings._FOOTERS` и
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
async def test_wizard_footer_names_tab_by_the_group(tmp_path):
    """Мастер: на «Своём endpoint» футер зовёт в поля, из полей — обратно к режиму."""
    from textual.widgets import Input
    from tests.tui.conftest import wizard_voice_step
    from tui.screens.onboarding import _TAB_TO_FIELDS, _TAB_TO_VARIANTS
    async with wizard_voice_step(tmp_path, mode="cloud") as (pilot, scr):
        # У «URL» свой пресет-лист (спека 2026-08-05 §3): пустое поле раскрывает его
        # само, и футер этот момент — про клавиши списка, а не про направление Tab
        # (см. _FOOTER_URL_LIST). Задаём НЕ-пресетный адрес, чтобы список молчал —
        # этот тест про сам разворот Tab, а не про список.
        scr.query_one("#voice-url", Input).value = "https://s.x/v1/audio/transcriptions"
        assert _TAB_TO_FIELDS in scr._footer_text()
        await pilot.press("tab")
        await pilot.pause()
        assert _TAB_TO_VARIANTS in scr._footer_text()
        assert _TAB_TO_FIELDS not in scr._footer_text()


@pytest.mark.asyncio
async def test_wizard_footer_keeps_quiet_about_tab_without_a_second_group(tmp_path):
    """«Без голосового ввода» полей не показывает: переходить некуда — Tab не обещан."""
    from tests.tui.conftest import wizard_voice_step
    async with wizard_voice_step(tmp_path, mode="skip") as (_pilot, scr):
        assert "Tab" not in scr._footer_text()


@pytest.mark.asyncio
async def test_wizard_footer_does_not_promise_space_from_inside_a_field(tmp_path):
    """В поле Space печатает пробел (Input съедает клавишу первым), пометки он там не
    ставит — обещать им выбор значит врать (§6.4)."""
    from textual.widgets import Input
    from tests.tui.conftest import wizard_voice_step
    async with wizard_voice_step(tmp_path, mode="cloud") as (pilot, scr):
        # НЕ-пресетный адрес — иначе список URL раскрывается сам (спека 2026-08-05 §3),
        # и футер на входе в поле — про клавиши списка, а не про «Enter — выбрать».
        scr.query_one("#voice-url", Input).value = "https://s.x/v1/audio/transcriptions"
        assert "Space" in scr._footer_text()
        await pilot.press("tab")
        await pilot.pause()
        assert "Space" not in scr._footer_text()
        assert "Enter — выбрать" in scr._footer_text()


@pytest.mark.asyncio
async def test_wizard_footer_names_the_right_key_per_step(tmp_path):
    """Развод клавиш (спека 2026-08-06 §2, §4.2): `Ctrl+Space` осталась только там,
    где есть проба (шаг «Подключение», карта проб §2.5), раскрытие/перечитывание
    списка ушло на `Alt+↓`. Шаг «Подключение» обещает обе — пробу и `Alt+↓ —
    показать список» (список свёрнут); шаг «Модель» — только `Alt+↓ — перечитать
    список», `Ctrl+Space` там больше не обещана; шаг MCP молчит про обе.

    Шаги адресуются ИМЕНАМИ (волна B, Task 8): состав шагов зависит от дискавери
    профилей, и номер «2» с этого момента у разных прогонов разный."""
    from tests.tui.conftest import wizard_connection_step
    from tui.screens.onboarding import STEP_CONNECTION, STEP_MCP, STEP_MODEL
    async with wizard_connection_step(tmp_path) as (pilot, scr):
        await pilot.press("escape")   # свернуть пресет-лист, раскрытый по умолчанию
        await pilot.pause()
        scr._step = scr._steps.index(STEP_CONNECTION)
        footer = scr._footer_text()
        assert "Ctrl+Space — проверить связь" in footer
        assert "Alt+↓ — показать список" in footer
        scr._step = scr._steps.index(STEP_MODEL)
        footer = scr._footer_text()
        assert "Alt+↓ — перечитать список" in footer
        assert "Ctrl+Space" not in footer
        scr._step = scr._steps.index(STEP_MCP)
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
async def test_wizard_footer_names_tab_after_a_bare_arrow(tmp_path):
    """Одна стрелка на «Свой endpoint» — и Tab уже обещан. Смена состава тела идёт
    через пересборку шага, и футер обязан считаться ПОСЛЕ монтажа полей: посчитанный
    раньше, он видит одну группу и молчит — ровно тот блокер, ради которого задача и
    делалась («поля вижу, войти нечем»)."""
    from tests.tui.conftest import wizard_voice_step
    from tui.screens.onboarding import _TAB_TO_FIELDS
    async with wizard_voice_step(tmp_path, mode="local") as (pilot, scr):
        assert _TAB_TO_FIELDS not in _footer_widget(scr)   # на «Локальном» полей нет
        await pilot.press("down")                          # → «Свой endpoint», поля видны
        await pilot.pause()
        assert _TAB_TO_FIELDS in _footer_widget(scr)


@pytest.mark.asyncio
async def test_wizard_model_list_footer_stops_promising_a_pick_without_matches(tmp_path):
    """Фильтр не оставил строк: стрелки съедены и никуда не двигают, Enter не
    подставляет (`pick_in` отдаёт False) — обещать «выбор модели» и «подставить»
    значит врать. Ввод своего id — штатный путь, а не край."""
    from textual.widgets import Input
    from tests.tui.conftest import wizard_voice_step
    async with wizard_voice_step(tmp_path, mode="cloud", models=["whisper-1"]) as (pilot, scr):
        scr.query_one("#voice-url", Input).value = "https://s.x/v1/audio/transcriptions"
        await pilot.press("tab", "down", "down")           # → поле «Модель», список раскрыт
        await pilot.pause()
        await pilot.pause()
        ed = scr._voice_endpoint_editor()
        assert ed.model_list.is_open and ed.model_list.has_matches
        assert "Enter — подставить" in _footer_widget(scr)
        scr.query_one("#voice-model", Input).value = "my-own-id"
        await pilot.pause()
        assert ed.model_list.is_open and not ed.model_list.has_matches
        footer = _footer_widget(scr)
        assert "выбор модели" not in footer and "Enter — подставить" not in footer
        assert "Esc — свернуть" in footer


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
    """Список раскрыт поверх своего адреса — подсвечивать нечего: Enter не подставит,
    а просто свернёт список и оставит набранное. Футер обязан звать клавишу так же.

    Путь до состояния — живой: набрали свой адрес (список свернулся, канон в редакции
    2026-08-18), затем сами позвали список `Alt+↓`. Раньше сюда приводила одна печать,
    но раскрытый пустой список забирал ↑/↓ и обещал Enter то, чего тот не делает."""
    from textual.widgets import Input
    from tests.tui.conftest import wizard_connection_step
    async with wizard_connection_step(tmp_path) as (pilot, scr):
        scr.query_one("#conn-url", Input).value = "https://my-own-host.internal/v1"
        await pilot.pause()
        await pilot.press("alt+down")
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
    смонтирован всё время шаг голоса. Для остальных шагов ветки, спрашивающие виджеты
    (раскрытый список, пресет-лист), молчат не потому, что состояние проверено, а
    потому что виджетов нет — их строки стережёт перебор констант и живые тесты выше.
    Здесь проверяется ровно словарь безусловных кусков сборки."""
    from tests.tui.conftest import wizard_voice_step
    async with wizard_voice_step(tmp_path, mode="cloud") as (pilot, scr):
        seen = []
        for step in range(len(scr._steps)):     # весь состав прогона, не «первые четыре»
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
    """Секция «Подключение» с ПУСТЫМ адресом открывается с раскрытым пресет-листом
    (чистая установка). Enter там подставляет провайдера и не сохраняет, Esc
    сворачивает список — футер «Enter — сохранить · Esc — меню» врал бы.

    Канон 2026-08-17: заполненный адрес список больше не поднимает, поэтому состояние
    задаётся пустым полем, а не совпадением с пресетом."""
    from config_modules.endpoint_presets import DEFAULT_ENDPOINT_PRESETS
    from tests.tui.conftest import settings_connection
    from tui.editors.connection import ConnectionEditor
    async with settings_connection(tmp_path, url="") as (_pilot, scr):
        assert scr.query_one(ConnectionEditor).presets_open
        footer = _set_footer_widget(scr)
        assert "Enter — подставить" in footer and "Esc — свернуть" in footer
        assert "Enter — сохранить" not in footer and "Esc — меню" not in footer


@pytest.mark.asyncio
async def test_settings_connection_footer_when_no_preset_matches(tmp_path):
    """Список раскрыт поверх своего адреса — подсвечивать нечего: Enter не подставит,
    а свернёт список и оставит набранное (те же слова, что у мастера).

    Входим со своим адресом — на непустом поле список свёрнут (канон), — и зовём его
    сами через `Alt+↓`. Печать сюда больше не приводит: без совпадений список
    сворачивается (редакция 2026-08-18)."""
    from config_modules.endpoint_presets import DEFAULT_ENDPOINT_PRESETS
    from tests.tui.conftest import settings_connection
    async with settings_connection(tmp_path,
                                   url="https://my-own-host.internal/v1") as (pilot, scr):
        await pilot.press("alt+down")
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
    async with settings_connection(tmp_path, url="") as (pilot, scr):
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


def test_models_section_footers_split_the_probe_from_the_list():
    """Пин переехал с `_FOOTERS[_MODEL]` на футеры секции «Модели»: секция поглотила
    легаси-«Модель», её футер стал позиционным и живёт в своём модуле.

    Развод клавиш (спека 2026-08-06 §2, контракт п.4 волны B) остался тем же, но
    теперь он ПОЗИЦИОННЫЙ: там, где под курсором список моделей и никакой пробы нет
    (легаси-редактор, он же под-форма резервной), обещан только `Alt+↓`; проба
    (`Ctrl+Space`) есть исключительно в форме записи реестра — у неё свои адрес и
    токен, которые и проверяются."""
    from tui.editors import models_section as m
    for footer in (m._FOOTER_EMPTY, m._FOOTER_RESERVE_FORM):
        assert "Alt+↓ — перечитать список" in footer
        assert "Ctrl+Space" not in footer
    assert "Ctrl+Space — проверить связь" in m._FOOTER_FORM_MODEL
    assert "Alt+↓ — перечитать список" in m._FOOTER_FORM_MODEL
    assert "Alt+↓ — показать список" in m._FOOTER_FORM_URL
    for footer in (m._FOOTER_ENTRY, m._FOOTER_RESERVE):
        assert "Alt+↓" not in footer and "Ctrl+Space" not in footer


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


def test_model_list_hint_calls_alt_down_by_the_canon_word():
    """Развод клавиш (спека 2026-08-06 §2, §2.1): раскрытие/перечитывание списка
    ушло на `Alt+↓` («перечитать список» — сетевой источник); `Ctrl+Space` в хинте
    списка моделей больше не называется, а «обновить версию» (Ctrl+U) остаётся
    отдельной ролью и со словом «список» не смешивается."""
    from tui.editors.voice_endpoint import _MODEL_HINT
    assert "Alt+↓ — перечитать список" in _MODEL_HINT
    assert "Ctrl+Space" not in _MODEL_HINT
    assert "обновить" not in _MODEL_HINT


def test_network_list_statuses_come_from_one_dictionary():
    """Статусы сетевого списка моделей (идёт загрузка / сбой / сервер вернул пусто) —
    ОДНА константа на оба экрана (`field_list.LIST_LOADING`/`LIST_FAILED`/`LIST_EMPTY`),
    а не текст, списанный в `model.py` и `voice_endpoint.py` порознь: до этой правки
    редакторы называли одни и те же три состояния разными словами и разошлись при первой
    же самостоятельной правке одного из экранов (находка владельца на живом смоке
    2026-08-06).

    Сторож сравнивает IDENTITY, а не текст: одинаковый текст двух РАЗНЫХ констант эту
    дыру не закрыл бы — экраны точно так же разъехались бы на следующей правке одного
    из них. `model.LIST_LOADING is field_list.LIST_LOADING` доказывает, что имя в
    `model.py` — это импорт, а не повторное объявление."""
    from tui.editors import field_list, model, voice_endpoint
    assert model.LIST_LOADING is field_list.LIST_LOADING
    assert model.LIST_FAILED is field_list.LIST_FAILED
    assert voice_endpoint.LIST_LOADING is field_list.LIST_LOADING
    assert voice_endpoint.LIST_FAILED is field_list.LIST_FAILED
    assert voice_endpoint.LIST_EMPTY is field_list.LIST_EMPTY


def test_url_error_comes_from_one_dictionary():
    """Отказ структурной проверки адреса — тоже ОДНА константа
    (`onboarding_fields.URL_ERROR`) на все формы с полем адреса, и по той же причине:
    списанный заново текст расходится с оригиналом на первой самостоятельной правке
    одной из форм. Сравнение по IDENTITY — одинаковый текст двух разных констант
    дыру не закрывает."""
    from onboarding_fields import URL_ERROR
    from tui.editors import connection, model_entry_form
    assert connection._URL_ERROR is URL_ERROR
    assert model_entry_form._URL_ERROR is URL_ERROR
