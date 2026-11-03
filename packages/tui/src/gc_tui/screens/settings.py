"""Хаб настроек /settings (спека B, модель A+; визуальный контракт §2-3).

Меню секций → Enter открывает тело секции (свап идиомой .set-dyn), Esc — назад
в меню (из меню — закрыть экран), ←/→ — соседняя секция БЕЗ выхода в меню
(только когда фокус не в текстовом поле). Push-экран поверх TraceScreen."""

from __future__ import annotations

import os
import time
from dataclasses import replace
from pathlib import Path
from typing import Literal, Optional

from gc_core.settings import runtime_of, settings_of
from gc_core.utils.path_display import home_path
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import Input, Rule, Static, TextArea

from gc_tui.editors.field_list import LIST_CLOSED_HINT
from gc_tui.editors.models_section import FOOTER_PROFILE_BACK
from gc_tui.editors.voice_endpoint import _MODEL_RELOAD_HINT, _URL_REQUIRED
from gc_tui.form_frame import (
    EPHEMERAL,
    FOOTER_SCROLL,
    KEEP_IN_VIEW,
    KEEP_UNTIL_DISMISSED,
    body_scrollable,
    cap_frame_height,
    reveal,
    reveal_focus,
    scroll_body,
)
from gc_tui.palette import ACCENT, DEEP_DIM, DIM, ERR, INK, OK, SUB_INK, WARN
from gc_tui.services.column_row import ColumnRow
from gc_tui.services.footer_slot import MAIN_FOOTER_MIN_COLS
from gc_tui.services.probe_presentation import connection_line_text
from gc_tui.services.profile_lock import profile_switch_refusal
from gc_tui.toggles import PROFILE_ACTIVE_STATUS, PROFILE_NONE_VALUE
from gc_tui.widgets.loading_wave import WAVE_INTERVAL, WAVE_SEQ, wave_glyph
from gc_tui.widgets.reserved_hint import ReservedHint

# Клавиша сохранения настроек.
_SAVE_KEY = "ctrl+s"

# Константы вместо повторяющихся литералов (Sonar S1192).
_SEL_SET_SCROLL = "#set-scroll"
_SEL_SET_FOOTER = "#set-footer"
_CLS_SELECTED = " selected"
_CLS_SET_HINT_SET_DYN = "set-hint set-dyn"
_CLS_SET_HINT_SET_DYN_SET_PROBE = "set-hint set-dyn set-probe"
_KEY_ALT_DOWN = "alt+down"
_KEY_CTRL_AT = "ctrl+at"
_KEY_CTRL_AT_SIGN = "ctrl+@"
_KEY_CTRL_DOWN = "ctrl+down"
_KEY_CTRL_SPACE = "ctrl+space"
_KEY_CTRL_U = "ctrl+u"
_MSG_APPLIES_NEXT_TURN = "применится со следующего хода агента"
_MSG_APPLIES_NEXT_START = "применится со следующего запуска"
_SEL_SET_FRAME = "#set-frame"
_SEL_MODES_HINT = ".set-modes-hint"
_SEL_SET_MENUROW = ".set-menurow"


SECTIONS = (
    "Подключение",
    "Модели",
    "Профили",
    "MCP-серверы",
    "Голосовой ввод",
    "Режимы работы",
    "Внешний вид",
    "Обновление",
)

_CONNECTION, _MODELS, _PROFILES, _MCP, _VOICE, _MODES, _LOOK, _UPDATE = (
    0,
    1,
    2,
    3,
    4,
    5,
    6,
    7,
)
# Секция «Внешний вид»: имя каждой темы `tui.theme` в порядке схемы и
# пояснение под строкой для каждого значения. Тема выбирается при старте до
# импорта приложения, поэтому запись действует со следующего запуска.
THEME_NAMES = {"auto": "Автоматическая", "gigacode": "GigaCode"}
_THEME_HINTS = {
    "auto": (
        "Подстраивается под фон вашего терминала и проверяет контраст "
        "текста. Если он слишком низкий, включается тема GigaCode."
    ),
    "gigacode": (
        "Фирменная тёмная тема. Выглядит одинаково в любом терминале."
    ),
}
_THEME_LABEL = "Тема"
_EDITOR_SECTIONS = {
    _CONNECTION
}  # редакторные: writable, dirty-tracked, submit
_PROBE_SECTIONS = {
    _CONNECTION
}  # server-probe: ручная проверка, глиф из config-probe
# Явная прокрутка тела секции — обещание `FOOTER_SCROLL`.
_SCROLL_KEYS = ("ctrl+up", "ctrl+down")
# Секции, пишущие в базовый config.yml: гейт пакетного шаблона общий, а вот
# save-машина — только у редакторных. «Модели» пишут немедленно по действию
# (решение 2 волны B), но в пакетный шаблон писать по-прежнему нельзя.
_WRITE_GATED_SECTIONS = _EDITOR_SECTIONS | {_MODELS}

_FOOTERS = {
    "menu": "↑/↓ — выбор  ·  Enter — открыть  ·  Esc — закрыть",
    # Пресет-лист адреса открывается сам только на пустом поле (канон 2026-08-17,
    # см. комментарий у `_FOOTER_PRESET_LIST` ниже) — а когда он свёрнут (адрес уже
    # задан, обычное состояние), футер обязан назвать способ вернуть его
    # (спека 2026-08-06 §4.4): без этого `Esc` уводил бы список в один конец без
    # обратного пути. Дописывание `Alt+↓ — показать список` вырастило строку со 108 до
    # 136 символов — на самом узком окне (`_FOOTER_MIN_COLS`, сторож
    # `test_all_footers_fit_the_two_line_budget`) она рвалась на ТРИ строки, и хвост
    # «Esc — меню» срезался высотой `#set-footer` (2). Жертва по правилу §4.2 спеки:
    # «↑/↓ — навигация» снята ПЕРВОЙ — курсор и так виден подсветкой поля (тот же довод,
    # что у `_VOICE_FOOTER_CLOUD_LIST`, §5.7 спеки), а не третьей (не называть клавишу) —
    # до неё дело не дошло, снятого сегмента хватило.
    _CONNECTION: (
        "Ctrl+S — сохранить/выйти · Alt+↓ — показать список · "
        "Ctrl+Space — проверить связь · Esc — меню"
    ),
    # Футера секции «Модели» здесь нет: он позиционный и живёт в самой секции
    # (`tui/editors/models_section.py`) — Enter значит разное на строке записи,
    # строке резервной модели и в каждой из её под-форм.
    # «Обновить версию», а не просто «обновить»: Ctrl+U ставит пакет заново (меняет
    # систему), а Alt+↓ только перечитывает — одним словом на двоих они неразличимы (§5.4).
    _MCP: "↑/↓ · Enter — установить · Space — вкл/выкл · Ctrl+U — обновить версию · Del — удалить · Esc",
    _MODES: "↑/↓ — выбор · ←/→ — изменить · PgUp/PgDn — секции · Esc — меню",
    # Строка одна — ↑/↓ не обещаем.
    _LOOK: "←/→ — изменить · PgUp/PgDn — секции · Esc — меню",
    _UPDATE: "↑/↓ · Enter — выполнить · Esc — меню",
    # Раскладка — решение юзера В1 (`docs/specs/2026-08-10-profiles-b-plan.md`),
    # выведенное из канона базы: Enter = «войти внутрь» (меню «открыть», MCP
    # «установить»), Space = переключение состояния (MCP «вкл/выкл», post-update
    # «отметить»). Активация профиля — смена состояния, поэтому её клавиша Space,
    # а Enter открывает редактор профиля (Task 6).
    # Стиль строки — MCP (точки-разделителями, без «Esc»): канонный хвост
    # «↑/↓ — выбор · … · Esc — меню» с пятью клавишами не влезает в две строки на
    # 64 колонках (бюджет `_FOOTER_MIN_COLS`).
    _PROFILES: (
        "↑/↓ · Enter — открыть · Space — активировать · n — создать · "
        "r — переименовать · Del — удалить"
    ),
}
_FOOTER_READONLY = "PgUp/PgDn — секции  ·  Esc — меню"
# Строка модели в «Режимах работы»: значение выбирается списком, ←/→ молчат.
_FOOTER_MODES_LIST = (
    "↑/↓ — выбор · Enter — список · PgUp/PgDn — секции · Esc — меню"
)
_FOOTER_MODES_LIST_OPEN = (
    "↑/↓ — выбор · Enter — записать · Esc — закрыть список"
)
# Отчёт операции (см. `_report`): своя ступень Esc-лестницы, и обещание про неё —
# и в самом отчёте (последней строкой), и в футере, пока он на экране.
_REPORT_DISMISS_HINT = "Esc — закрыть отчёт"
_FOOTER_REPORT = "Esc — закрыть отчёт"
# Раскрытый пресет-лист адреса забирает клавиши себе — футер на это время становится
# его (§6.4, канон мастера `onboarding._FOOTER_PRESET_LIST`). Состояние редкое: канон
# 2026-08-17 поднимает список сам только на ПУСТОМ поле (чистая установка), а поверх
# заданного адреса — лишь по явному Alt+↓. У заполненного адреса стрелки остаются
# у формы, и способ вызвать список назван строкой под полем.
# «↑/↓ — выбор», а не «выбор провайдера» (как зовёт то же место мастер): у `#set-footer`
# ширина минус padding, и полная формулировка давала ровно 120 — на канонных 120
# колонках хвост «секции» срывался на вторую строку в самом частом состоянии секции.
# Слово то же, что у списка моделей рядом (секция «Модели»), а что именно выбирают,
# видно по строкам под полем.
_FOOTER_PRESET_LIST = "↑/↓ — выбор  ·  Enter — подставить  ·  Esc — свернуть  ·  Ctrl+Space — проверить связь  ·  PgUp/PgDn — секции"
# Набран свой адрес — в списке не осталось строк: подставлять нечего (`pick()` вернёт
# None), Enter лишь сворачивает список, оставляя набранное, а ↑/↓ снова водят курсор
# секции (`ConnectionEditor.move` отдаёт их ленте). Глагол — тот же, что в хинте под
# рамкой (`preset_list.PRESET_HINT_NO_MATCH`): обе строки видны одновременно.
_FOOTER_PRESET_LIST_NO_MATCH = "↑/↓  ·  Enter — оставить свой адрес  ·  Esc — свернуть  ·  Ctrl+Space — проверить связь  ·  PgUp/PgDn — секции"
# Список свёрнут, но курсор НЕ на адресе (Токен/Прокси/галка): Alt+↓ там молчит —
# `ConnectionEditor.on_url_field` гейтит ровно позицию адреса (`reload_presets`), и
# `_FOOTERS[_CONNECTION]` в этом состоянии обещал бы неработающую клавишу (находка
# финального ревью 2026-08-06, D). Тот же текст без сегмента про Alt+↓.
_FOOTER_CONNECTION_FIELD = (
    "Ctrl+S — сохранить/выйти · Ctrl+Space — проверить связь · Esc — меню"
)
# Голосовая секция: у неё свой билдер футера (`_voice_footer_text`), в общем словаре
# записи нет. Бюджет ширины у неё общий со всеми футерами форм — `_FOOTER_MIN_COLS`
# ниже (то же число и тот же способ замера: не длина строки, а перенос на самом узком
# окне).
# Обещание Tab. Клавиша водит курсор МЕЖДУ группами (§4.1), поэтому честный глагол
# зависит от того, где курсор стоит сейчас: из строк режима — «вниз к настройкам», из
# полей — обратно «вверх к выбору». Направление названо словом: без него надпись
# говорила «в поля» и не отвечала на вопрос куда (смок владельца 2026-08-05).
# «Настройки», а не «поля ввода»: у локального режима ниже не поля, а факты установки и
# выбор языка. Полное «вверх к выбору режима» не взято — облачный футер с ним доходит
# до 129 символов и на окне 64 колонки теряет хвост «Esc — меню» (замер переносом).
# Тот же словарь у мастера (`onboarding._TAB_TO_*`) — одна клавиша
# не может называться на двух экранах по-разному; сторож — test_footer_contract.
_TAB_TO_FIELDS = "Tab — вниз к настройкам"
_TAB_TO_VARIANTS = "Tab — вверх к выбору"
# Футер зависит от панели под курсором — Ctrl+Space проверяет только свой
# endpoint, а Enter на «Языке» локального режима сохраняет язык, а не включает режим.
# Del и Esc — короткой формой MCP-секции («удалить» о строке под курсором, «Esc» без
# хвоста): в развёрнутой форме версия с Ctrl+U не влезала в две строки бюджета футера
# на 64 колонках (сторож test_all_footers_fit_the_two_line_budget), а два состояния
# ОДНОЙ строки обязаны звать одни клавиши одними словами.
_VOICE_FOOTER_LOCAL = (
    f"↑/↓ · {_TAB_TO_FIELDS} · Enter — включить · Del — удалить · Esc"
)
# Тот же футер, когда движок отстал от версии сборки: клавиша обещается ТОЛЬКО тогда
# (канон секции — не звать клавишу, которая здесь ничего не делает). Глагол и клавиша —
# как у MCP («Ctrl+U — обновить версию»), там этот сценарий уже живёт.
_VOICE_FOOTER_LOCAL_UPDATE = f"↑/↓ · {_TAB_TO_FIELDS} · Enter — включить · Ctrl+U — обновить версию · Del — удалить · Esc"
# Тот же футер, когда удалять нечего: сервиса нет, и Del молчит (`_voice_request_remove`).
# Полей у такой строки тоже нет — переходить некуда, и Tab здесь не обещан (§6.1).
_VOICE_FOOTER_LOCAL_FRESH = "↑/↓ · Enter — установить · Esc — меню"
# Облачная панель: Del здесь не работает вовсе — он стреляет только на строке
# «Локальный сервис». Обещать его значит врать (та же причина, по которой из локального
# футера убран Ctrl+Space).
_VOICE_FOOTER_CLOUD = (
    f"↑/↓ · {_TAB_TO_FIELDS} · Ctrl+S — сохранить и выйти · "
    "Ctrl+Space — проверить связь · PgUp/PgDn — секции · Esc — меню"
)
# Та же облачная панель, но курсор уже в поле БЕЗ списка (осталось одно — «Токен»,
# у URL/«Модели»/«Языка» свой футер ниже, `_VOICE_FOOTER_CLOUD_LIST`): клавиши те же,
# а Tab ведёт в обратную сторону — ради одного этого слова футер и раздваивается.
_VOICE_FOOTER_CLOUD_FIELD = (
    f"↑/↓ · {_TAB_TO_VARIANTS} · Ctrl+S — сохранить и выйти · "
    "Ctrl+Space — проверить связь · PgUp/PgDn — секции · Esc — меню"
)
# Тот же «Токен», но под маской лежит СОХРАНЁННЫЙ секрет — Ctrl+U его стирает (канон
# «Подключения», `_FOOTER_CONNECTION_TOKEN`). Обещаем ровно там, где клавише есть что
# делать: на пустом поле она молчит, и называть её было бы ложью.
# Место под новый сегмент берём у «↑/↓» — по правилу §4.2 жертвуем тем, что человек и
# так пробует первым, а не работающей клавишей; в две строки на 64 колонках иначе не
# уложиться (сторож `test_all_footers_fit_the_two_line_budget`).
_VOICE_FOOTER_CLOUD_TOKEN = (
    f"{_TAB_TO_VARIANTS} · Ctrl+S — сохранить и выйти · "
    "Ctrl+U — очистить · Ctrl+Space — проверить связь · "
    "Esc — меню"
)
# Список свёрнут, курсор в поле «Язык»: раскрыть нечем стрелками — Alt+↓ делает это
# явно (второй путь после печати, см. FieldListController.reopen_for_typing; роль
# ушла с Ctrl+Space — спека 2026-08-06 §2, §4.1). Про Del
# здесь молчим по той же причине, что и в облачных списковых полях ниже: гейт «фокус в
# поле» его и так гасит, а `_voice_request_remove` стреляет только на строке
# «Локальный сервис» — обещание было прямой ложью (спека §2.11).
# Глагол — дословно `LIST_CLOSED_HINT`: эта самая строка стоит сейчас под полем
# (её держит `FieldListController._sync_hint`), и звать одну клавишу двумя словами
# на одном экране нельзя (§5.4).
_VOICE_FOOTER_LOCAL_LANG = f"↑/↓ · {_TAB_TO_VARIANTS} · Ctrl+S — сохранить и выйти · {LIST_CLOSED_HINT} · Esc — меню"
# То же для полей со списком («Язык», «Модель», «URL» — спека 2026-08-05 §3) облачной
# панели. Про Del здесь НЕ говорим: во-первых, футер — две строки без скролла, и место
# тесное; во-вторых, на этих полях Del и не сносит сервис — `_voice_request_remove`
# стреляет только на строке «Локальный сервис». Ctrl+Space дописан (спека 2026-08-06
# §4.2): список свёрнут, но раскрытие больше не «съедает» клавишу (§2.2) — она всё
# равно проверит связь, и молчать об этом значит не назвать работающую клавишу.
# Бюджет ширины (сторож `test_all_footers_fit_the_two_line_budget`) не
# оставляет места на всё сразу: `LIST_CLOSED_HINT` и обе канон-фразы Ctrl+Space —
# фиксированные слова словаря (белый список `test_both_screens_call_ctrl_space_by_the_canon_words`),
# резать нечем. Глагол Enter НЕ трогаем — «сохранить и включить» именован владельцем
# осознанно (`_voice_cloud_save`: Enter здесь и включает облачный режим, а не только
# пишет поле, §5.4 канона требует одно слово на одно действие во всей панели). Жертва
# по §4.2 спеки — «↑/↓» (курсор и так виден подсветкой поля) и «PgUp/PgDn — секции»
# (та же клавиша названа в футере ЛЮБОЙ другой секции `/settings`, эта строка не
# единственный источник о ней) — постоянное, а не временное отклонение, записано §5.7
# спеки.
_VOICE_FOOTER_CLOUD_LIST = (
    f"{_TAB_TO_VARIANTS} · Ctrl+S — сохранить и выйти · "
    f"{LIST_CLOSED_HINT} · Ctrl+Space — проверить связь · "
    "Esc — меню"
)
# То же самое, но курсор на «Модели»: список сетевой, Alt+↓ и в свёрнутом состоянии
# идёт на сервер — глагол тот же, что в открытом (`_MODEL_HINT`, «перечитать
# список»), иначе на этом поле клавиша звалась бы двумя глаголами в зависимости от
# того, свёрнут список или нет (находка финального ревью 2026-08-06, C: то же
# рассогласование, что чинит `closed_hint` у `ModelListController`, — здесь его
# двойник в футере секции).
_VOICE_FOOTER_CLOUD_LIST_MODEL = (
    f"{_TAB_TO_VARIANTS} · Ctrl+S — сохранить и выйти · "
    f"{_MODEL_RELOAD_HINT} · Ctrl+Space — проверить связь · "
    "Esc — меню"
)
# Раскрыт список: клавиши места — его. ↑/↓ ходят по списку (курсор стоит), Esc сворачивает
# список, а не выходит в меню, Del правит текст в поле — обещать их нельзя. Tab здесь
# работает (сворачивает список и уводит в другую группу, §4.2.4), но не назван: эти три
# строки — про СПИСОК, а Esc возвращает к футеру поля, где Tab обещан явно.
# Ctrl+Space (проба) дописан ТОЛЬКО в облачную версию: список забирает себе лишь
# Esc/↑↓/Enter (§2.2 спеки 2026-08-06), а проба живёт своим воркером и раскрытому
# списку не мешает — молчать о ней значит не назвать клавишу, которая здесь и правда
# работает. В локальной панели пробы нет вовсе (§9.2 канона, §5.3 спеки) — обещать её
# там значило бы соврать. Языковое поле — единственное общее между панелями (модели и
# URL раскрываются только у облака), поэтому константа и раздвоена: без раздвоения
# локальная панель получила бы чужое обещание проверки связи.
_VOICE_FOOTER_LANG_LIST_LOCAL = "↑/↓ — выбор языка · Enter — подставить · Ctrl+S — сохранить и выйти · Esc — свернуть"
_VOICE_FOOTER_LANG_LIST_CLOUD = (
    "↑/↓ — выбор языка · Enter — подставить · Ctrl+S — сохранить и выйти · "
    "Esc — свернуть · Ctrl+Space — проверить связь"
)
_VOICE_FOOTER_MODEL_LIST = (
    "↑/↓ — выбор модели · Enter — подставить · Ctrl+S — сохранить и выйти · "
    "Esc — свернуть · Ctrl+Space — проверить связь"
)
# Пресеты адреса (спека 2026-08-05 §3): глагол — тот же, что у языка/моделей выше,
# «подставить и сохранить» — Enter после подстановки так же безусловно доезжает до
# `_voice_cloud_save` (`_voice_enter`), место в раскладке общее, и разный текст читался
# бы как разная клавиша. ЧУЖОЙ пресет тем же Enter'ом стирает поле «Модель» (§4.1) и
# упирается в ошибку формы «выберите модель» — но это штатный путь валидации (курсор
# ленты сам встаёт на названное поле, см. `_voice_cloud_save`), а не повод не обещать
# сохранение: у языка/модели свой брак ввода тоже не сохраняется, и это не мешает им
# звать Enter «сохранить». Модели и URL раскрываются только у облака (§2.5 спеки:
# пробы у локальной панели нет), поэтому эти два футера раздваивать не на что —
# Ctrl+Space здесь всегда честен.
_VOICE_FOOTER_URL_LIST = (
    "↑/↓ — выбор адреса · Enter — подставить · Ctrl+S — сохранить и выйти · "
    "Esc — свернуть · Ctrl+Space — проверить связь"
)
# Строк под фильтром нет (или список ещё грузится / не загрузился): подставлять нечего
# (футер не спорит с хинтом списка), а ↑/↓ снова про курсор секции — ходить нечем.
# Раздвоена по той же причине, что и языковой список выше: состояние «без совпадений»
# достижимо и у локального языка (пустой фильтр), и там пробы по-прежнему нет.
_VOICE_FOOTER_LIST_NO_MATCH_LOCAL = "↑/↓ · Enter — оставить как есть · Ctrl+S — сохранить и выйти · Esc — свернуть"
_VOICE_FOOTER_LIST_NO_MATCH_CLOUD = "↑/↓ · Enter — оставить как есть · Ctrl+S — сохранить и выйти · Esc — свернуть · Ctrl+Space — проверить связь"
_VOICE_LANG_ERROR = "не сохранено: код языка — 2-3 латинские буквы, напр. ru"

# Секция «Голосовой ввод»: две строки режима (язык радио-опций мастера).
_VOICE_ROWS = ("Локальный сервис", "Свой endpoint")
_VOICE_ROW_LOCAL, _VOICE_ROW_CLOUD = 0, 1
_VOICE_MODE_ROW = {
    "local": _VOICE_ROW_LOCAL,
    "cloud": _VOICE_ROW_CLOUD,
}  # off → ни одной ●
# Курсор секции ходит ГРУППАМИ (спека §4.1, канон `OnboardingScreen`): строки режима и
# поля показанной панели — две независимые группы, стрелки двигают внутри группы, Tab
# переносит между ними. Позиция у каждой группы своя и помнится отдельно.
_VOICE_GROUP_VARIANTS = "variants"
_VOICE_GROUP_FIELDS = "fields"
_VOICE_FIELDS = ("url", "token", "model", "language")
# Поля с раскрывающимся списком (Alt+↓, свои футеры): язык — константа, модели —
# ответ самого endpoint'а, адрес — пресеты (спека 2026-08-05 §3). В локальной панели
# курсор на «Модель»/«URL» не встаёт (поля-факты) — список там и не строится
# (`VoiceEndpointEditor.url_list`/`model_list` гейтятся readonly).
_VOICE_LIST_FIELDS = ("language", "model", "url")
# Локальный режим показывает поля-факты (их правит установка сервиса, не человек);
# редактируется только язык распознавания.
_VOICE_LOCAL_READONLY = ("url", "token", "model")
_VOICE_LOCAL_TOKEN = "не требуется"
# Описания режимов: что это и чем платишь (клавиши — в футере, канон §6). Без примеров
# провайдеров (шум: адрес всё равно свой) и без правила про пустой Token: маска
# сохранённого значения в самом поле уже говорит, что там что-то есть.
_VOICE_MODE_HINTS = {
    # Цена в ресурсах — теми же словами, что на шаге голоса в мастере: один сервис не
    # должен описываться в двух местах по-разному (замер на faster-whisper-small).
    "local": (
        "Распознавание речи прямо на вашем компьютере: сервис speaches + "
        "модель {model} (~0.9 ГБ на диске, ~1 ГБ RAM, CPU до 4 потоков "
        "при распознавании)."
    ),
    "cloud": (
        "Распознавание речи на внешнем сервисе по OpenAI-совместимому API. Нужны адрес и, обычно, токен."
    ),
}
# Разбор подсказок в теле (§6.2): эта — ОСТАЁТСЯ. Она стоит на месте полей и отвечает
# на вопрос ровно там, где он возникает: «полей нет, потому что сервиса нет; ставит его
# Enter». Футер говорит за всю секцию и показать на место не умеет — потому строка и не
# сводится к нему, хотя тот же Enter называет и он (`_VOICE_FOOTER_LOCAL_FRESH`).
_VOICE_INSTALL_HINT = "Enter — установить"
# Прокси агента живёт на «Подключении», рядом с адресом модели — там связь видна
# глазом. У голоса свой экран и свой адрес, и то, что наружу он идёт тем же путём,
# угадать неоткуда. Показываем ТОЛЬКО когда прокси задан: иначе строка ни о чём.
_VOICE_PROXY_HINT = "запросы — через прокси агента (секция «Подключение»)"
# Строка «действует другое — перекрыто …»: классы канона _mount_field_hints.
_VOICE_OVERRIDE_CLASSES = "set-hint set-dyn set-warnflash set-voiceoverride"
# Курсор на токене и есть что стирать. Сегмент занимает место «Alt+↓ — показать
# список» — тот обещается только на адресе, эти двое на разных полях и в одной
# строке не встречаются, поэтому бюджету ширины ничего не грозит.
_FOOTER_CONNECTION_TOKEN = "Ctrl+S — сохранить/выйти · Ctrl+U — очистить · Ctrl+Space — проверить связь · Esc — меню"
_FOOTER_DIRTY_CONFIRM = "Enter — выйти без сохранения  ·  Esc — остаться"
# Строка «— без профиля» — не объект, а действие «снять текущий выбор», и глагол
# у неё СВОЙ. Общий футер секции обещал здесь «активировать», пока подсказка под
# списком в том же кадре говорила «деактивировать»; курсор при входе в секцию
# стоит ровно на этой строке, так что противоречие было первым, что видит глаз.
# Тот же позиционный приём, что у «Подключения» (_FOOTER_CONNECTION_FIELD).
# «n — создать» здесь и в пустом варианте ниже остаётся: клавиша секционная (не
# про строку под курсором) и работает на любой строке, включая ПУСТОЕ состояние,
# где создавать первый профиль больше нечем. Футер, промолчавший о работающей
# клавише, — второй класс лжи, и стоит он дороже сэкономленного места.
_FOOTER_PROFILES_NONE = "↑/↓ — выбор  ·  Space — деактивировать  ·  n — создать  ·  PgUp/PgDn — секции  ·  Esc — меню"
# Профиль не выбран — снимать нечего, и футер молчит о деактивации: обещать
# «деактивировать» там, где деактивировать некого, — та же ложь другим глаголом.
_FOOTER_PROFILES_NONE_EMPTY = (
    "↑/↓ — выбор  ·  n — создать  ·  PgUp/PgDn — секции  ·  Esc — меню"
)
# Строка-действие «+ добавить профиль…»: обещаем только то, что на ней работает.
_FOOTER_PROFILES_ADD = (
    "↑/↓ — выбор  ·  Enter — создать  ·  PgUp/PgDn — секции  ·  Esc — меню"
)
# Инлайн-ввод имени профиля забирает клавиши себе (контракт п.5 волны B) — футер
# на это время про него, и глагол называет ровно то, что произойдёт.
_FOOTER_PROFILE_NAME_CREATE = "Enter — создать  ·  Esc — отмена"
_FOOTER_PROFILE_NAME_RENAME = "Enter — переименовать  ·  Esc — отмена"
# Строка «— без профиля» — не файл: «Enter — открыть» футера на ней не сбывается,
# и молчать об этом нельзя (решение В1: no-op + подсказка).
_PROFILE_NONE_NOTHING_TO_OPEN = "открывать нечего — это не файл профиля"
# Бюджет ширины футера — `#set-footer` две строки БЕЗ скролла (тот же контракт у
# `#onb-footer` мастера): что не влезло, то отрезано вместе с хвостом «Esc — меню».
# Меряется он ПЕРЕНОСОМ на самом узком окне, а не числом символов: перенос идёт по
# словам, и длинный сегмент («Ctrl+Space — проверить связь») роняет строку раньше, чем
# счётчик символов это заметит. 64 — самое узкое окно из тех, на которых гоняются
# экраны форм (сторож test_all_footers_fit_the_two_line_budget).
_FOOTER_MIN_COLS = MAIN_FOOTER_MIN_COLS
_TITLE_NOTE_SEC = 3.0  # «ничего не изменено» у заголовка гаснет само


def fit_mode_pairs(pairs: list[str], width: int) -> str:
    """Превью секции «Режимы работы»: пары `label: value`, соединённые ` · `,
    усечённые ЦЕЛЫМИ парами под `width` (дизайн §2) — показываем столько
    полных пар, сколько влезает вместе с хвостом ` · …`, пара не режется
    посередине. `width <= 0` (строка ещё не смонтирована) — без усечения."""
    full = " · ".join(pairs)
    if width <= 0 or len(full) <= width:
        return full
    tail = " · …"
    out: list[str] = []
    for pair in pairs:
        candidate = " · ".join(out + [pair])
        if len(candidate) + len(tail) <= width:
            out.append(pair)
        else:
            break
    return " · ".join(out) + tail if out else "…"


# Отступ хвоста строки профиля (слоты и секции-оверлеи) от колонки источника
# и колонки «активен» от описания.
_PROFILE_TAIL_GAP = 3


# Подпись режима короче — уже не читается: дальше строка переносится как есть.
_MODE_HEAD_MIN = 8


def fit_row_head(row: Text, head_end: int, tail_w: int, width: int) -> Text:
    """Первая строка ``row`` — курсор (2 клетки), голова до ``head_end`` и
    хвост (трек, значение). Не влезает в ``width`` — режется голова с «…»,
    хвост цел: строка режима не переносится (узкое окно). Вторая строка
    («в текущей сессии») не трогается; ``width`` 0 — ширины ещё нет."""
    room = width - 2 - tail_w
    if width <= 0 or room >= head_end - 2 or room < _MODE_HEAD_MIN:
        return row
    lines = row.split("\n", allow_blank=True)
    line = lines[0]
    head = line[2:head_end]
    head.rstrip()
    head.truncate(room - 1, overflow="ellipsis")
    head.pad_right(room - head.cell_len)
    fitted = Text.assemble(line[:2], head, line[head_end:])
    return Text("\n").join([fitted, *lines[1:]])


def _shown_path(path) -> str:
    """Путь файла в репликах экрана: домашний каталог — через `~`."""
    return home_path(str(path), os.path.expanduser("~"))


def _without_path_prefix(message: str, path) -> str:
    """Снять с сообщения ведущий «<путь>: », если путь тот же.

    Отказы чтения приходят самодостаточными («<путь>: не удалось прочитать: …») —
    такими их печатают те, кто показывает их в одиночку (объяснение сломанного
    конфига, отказ записи реестра). В блоке профиля путь уже стоит первой
    строкой, и дословный текст давал его дважды подряд; на экране это читалось
    как повтор, а не как уточнение. Путь чужой — не трогаем: значит сообщение
    про ДРУГОЙ файл, и срезать его было бы враньём.
    """
    prefix = f"{path}: "
    return message[len(prefix) :] if message.startswith(prefix) else message


def _empty_anchor(value) -> bool:
    """Пустой якорь секции файла профиля: `slots: {}` (то, что остаётся после
    снятия последнего ключа) либо голое `slots:` без значения. Переопределением
    такая секция не является — переопределять ей нечем (§11).

    Скаляр пустым якорем не бывает НИКОГДА, каким бы он ни был: `level: max` —
    полноценное переопределение, и трактовать его как контейнер значило бы
    потерять единственную витрину ключей, форм для которых нет."""
    return value is None or (isinstance(value, (dict, list)) and not value)


class SettingsScreen(Screen):
    def __init__(
        self,
        *,
        settings,
        config_path,
        on_apply,
        probe_fn=None,
        auth_probe_fn=None,
        voice=None,
    ) -> None:
        super().__init__()
        # База окна (`LoadedSettings`, без сессионного слоя) — для витрин. Правка
        # и запись config.yml идут сырыми документами (`_read_raw`).
        self._settings = settings
        self._config_path = config_path
        self._raw = (
            self._read_raw()
        )  # значения полей ИЗ ФАЙЛА, без резолва (см. _read_raw)
        self._origins = (
            self._read_origins()
        )  # происхождение полей: чем перекрыто, что действует
        self._on_apply = on_apply
        self._pending_env_save = None  # (section, values, warn_detail, back_to_menu) — ждёт confirm перезаписи ссылки
        self._dirty_exit_confirm = False
        self._dirty_exit_focus = None
        self._pending_section_delta = None
        self._view: object = "menu"  # "menu" | int(0..5)
        self._menu_idx = 0
        self._modes_idx = 0  # курсор строки подэкрана «Режимы работы»
        # Открытый список моделей строки `model` (Enter); перерисовка
        # секции его снимает вместе с остальным телом рамки.
        self._modes_picker = None
        self._profiles_idx = 0  # курсор строки подэкрана «Профили»
        self._profiles_disk_cache: dict = {}  # дисковые чтения экрана на один проход UI
        # Под-состояния секции «Профили» (волна B): открытый инлайн-ввод имени
        # ({"mode": "create"|"rename", "original": имя|None}), имя профиля,
        # ждущего подтверждения удаления, и имя ОТКРЫТОГО в редакторе профиля
        # (Task 6). Все три — по одному полю, как `_pending` секции MCP: «висит ли
        # баннер»/«открыт ли под-вид» выводится из наличия значения. Хранится ИМЯ, а
        # не ref/путь: набор строк перечитывается с диска на каждый кадр, и файл мог
        # уехать под ногами — резолвить имя надо там же, где рисуем.
        self._profiles_name_prompt = None
        self._profiles_delete_name = None
        self._profiles_open = None
        self._menu_pass_snapshot = (
            None  # (текст, документ) цели записи на один проход меню
        )
        self._models_verdict_cache = (
            None  # (снимок файлов стека, вердикт) — см. _models_verdict
        )
        self._parsed_target = (
            None  # (текст, документ) цели записи — см. _target_snapshot
        )
        from gc_tui.services.settings_probe import probe as _default_probe
        from gc_tui.services.settings_probe import (
            probe_auth as _default_auth_probe,
        )

        self._probe_fn = probe_fn or _default_probe
        # Отдельно от _probe_fn: тому нужна models-цепочка (глифы хаба показывают
        # модели сервера), а вердикт об авторизации обязан идти строго по
        # chat/completions — у публичного /models он получился бы ложным.
        #
        # Подменили probe_fn — значит забрали сеть целиком, и вторым каналом наружу
        # экран не ходит: иначе частично застабленный экран молча стучится в интернет.
        self._auth_probe_fn = auth_probe_fn or probe_fn or _default_auth_probe
        self._probe_config = (
            None  # ProbeResult сохранённых значений → глифы хаба
        )
        self._config_gen = (
            0  # generation config-probe: устаревшие результаты отбрасываются
        )
        self._draft_gen = (
            0  # generation draft-probe: независим от config-probe
        )
        self._autoprobe_timer = None  # таймер автопробы секции «Подключение» (см. _cancel_autoprobe)
        self._last_probed_snapshot = (
            None  # ConnectionEditor.probe_snapshot() последней пробы
        )
        self._probe_in_flight = False
        self._note_timer = None  # автогашение title-note «ничего не изменено»
        self._wave_timer = None  # таймер анимации загрузки (глифы хаба)
        self._wave_i = 0  # кадр «бегущего огонька»
        # draft-слот: результат последней проверки введённых значений
        # (probe-строка рендерится напрямую в _on_draft_probe; умный Enter: ok → без повтора)
        self._draft_checked = None
        # секция «Обновление»: список действий текущего рендера + курсор (None → дефолт
        # при следующем _mount_update_section — верхнее действие, «Обновить агента»)
        self._update_actions: list[str] = []
        self._update_idx = None
        self._update_check_result = (
            None  # CheckResult последней проверки (Task 7)
        )
        self._update_checking = False  # воркер check_for_update в полёте
        self._update_awaiting_confirm = (
            False  # confirm-баннер обновления/рестарта висит
        )
        # секция «Голосовой ввод»: фасад инъекцией (паттерн «мастер ↔ voice_setup»,
        # см. tui.screens.onboarding), список действий текущего рендера + курсор + confirm удаления
        if voice is None:
            from gc_tui.services import voice_setup as voice_mod

            self._voice_setup = voice_mod
        else:
            self._voice_setup = voice
        self._voice_group = (
            _VOICE_GROUP_VARIANTS  # группа под курсором (спека §4.1)
        )
        self._voice_row = _VOICE_ROW_LOCAL  # позиция в группе строк режима
        self._voice_field_idx = (
            0  # позиция в группе полей — своя у каждой группы
        )
        self._voice_pane = (
            "local"  # чьи поля показаны — строки режима под курсором
        )
        self._voice_cursor_placed = (
            False  # дефолт-курсор ставится один раз, на первом входе
        )
        self._voice_cloud_draft = (
            None  # набранное в полях endpoint (переживает смену панели)
        )
        self._voice_mounted_fields = (
            None  # сигнатура набора полей ТЕКУЩЕГО рендера
        )
        self._voice_status = (
            None  # снимок статуса ТЕКУЩЕГО рендера (гейты клавиш)
        )
        self._voice_snapshot_value = (
            None  # кэш снимка статуса (см. _voice_snapshot)
        )
        self._voice_snapshot_at = 0.0  # монотонная метка кэша
        self._voice_alive = None  # живость порта: None = ещё не мерялась
        self._voice_alive_gen = 0  # поколение пробы живости (гонки воркеров)
        self._voice_awaiting_remove = False
        self._voice_awaiting_install = False
        self._voice_probe_result = (
            None  # ProbeResult последней проверки endpoint'а
        )
        self._voice_probe_values = (
            None  # значения полей, к которым относится вердикт
        )
        self._voice_probe_in_flight = False
        self._voice_probe_gen = (
            0  # generation probe-воркера: устаревший результат отбрасывается
        )
        self._voice_list_reopen_gate = False  # см. _sync_voice_field_lists: сохранение не раскрывает список заново
        self._reset_awaiting_confirm = (
            False  # confirm-баннер «сбросить конфиг к умолчанию»
        )
        # Открытый ОТЧЁТ операции (`_report`) — состояние экрана, а не запрос к DOM:
        # футер обновляется в том же такте, где отчёт монтируется, а смонтированный
        # виджет `query` в этот момент ещё не находит. Хранится текстом, чтобы
        # пережить перерисовку своей секции (стрелки её вызывают).
        self._report_text: str | None = None
        self._report_section = None

    # --- хром ---
    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            with Horizontal(id="set-header"):
                yield Static(
                    self._title_text(), id="header-left", markup=False
                )
                yield Static(
                    self._version_text(), id="header-right", markup=False
                )
            # Фокус у области прокрутки не отнимаем (Р17): на нём держится
            # клавиатурная прокрутка. Свои клавиши экран забирает раньше —
            # `on_key` предка отрабатывает ДО проверки биндингов, — поэтому
            # ↑/↓ и PgUp/PgDn остаются за меню и секциями, а контейнеру
            # достаётся только то, что экран не занял (Home/End и прочее).
            with VerticalScroll(id="set-scroll"):
                with Vertical(id="set-frame"):
                    yield Static("Настройки", id="set-title", markup=False)
            yield Static(self._footer_text(), id="set-footer", markup=False)

    def _version_text(self) -> str:
        """Строка версии в шапке. Короткая форма (`dev`, а не полная метка): строка
        тесная. Но правило то же, что в секции «Обновление» — из исходников номер не
        показываем, он остался от установки пакета и коду не соответствует."""
        from gc_core import app_version

        return f"gc-ua · {app_version.display_version()}"

    def _title_text(self) -> str:
        if self._view == "menu":
            return "Настройки"
        return f"Настройки · {SECTIONS[self._view]}"

    def _footer_base(self) -> str:
        if self._dirty_exit_confirm:
            return _FOOTER_DIRTY_CONFIRM
        if self._report_open():
            # Отчёт держит экран: единственное, что сейчас работает, — его закрытие.
            return _FOOTER_REPORT
        if self._view == "menu":
            return _FOOTERS["menu"]
        if self._view in _WRITE_GATED_SECTIONS and not self._writable():
            return _FOOTER_READONLY
        if self._view == _MODELS:
            # Футер секции — её собственный и позиционный (Enter значит разное на
            # строке записи, строке резервной и в под-формах): экран его не считает,
            # а спрашивает у секции. Редактора ещё нет (первый кадр) — молчим.
            ed = self._models_editor()
            return ed.footer_text() if ed is not None else _FOOTER_READONLY
        if self._view == _VOICE:
            return self._voice_footer_text()
        if self._view == _MODES and self._modes_picker is not None:
            return _FOOTER_MODES_LIST_OPEN
        if self._view == _MODES and self._modes_row_opens_picker():
            return _FOOTER_MODES_LIST
        if self._view == _CONNECTION and self._presets_open():
            return (
                _FOOTER_PRESET_LIST
                if self._presets_have_selection()
                else _FOOTER_PRESET_LIST_NO_MATCH
            )
        if self._view == _CONNECTION:
            # Список свёрнут: Alt+↓ обещаем, только пока курсор на адресе — на «Токене»/
            # «Прокси» клавиша молчит (`on_url_field` гейтит ровно эту позицию,
            # находка финального ревью 2026-08-06, D), и обещать её там значило бы врать.
            if self._connection_on_url():
                return _FOOTERS[_CONNECTION]
            return (
                _FOOTER_CONNECTION_TOKEN
                if self._connection_can_clear_token()
                else _FOOTER_CONNECTION_FIELD
            )
        if self._view == _PROFILES:
            if self._profiles_open is not None:
                # Открытый профиль — тот же редактор форм, что и «Модели», и футер у
                # него тот же позиционный: экран его не считает, а спрашивает. Пока
                # тело не смонтировано — обещаем только возврат к списку:
                # `_FOOTER_READONLY` соврал бы про «Esc — меню».
                ed = self._models_editor()
                return (
                    self._profile_footer(ed)
                    if ed is not None
                    else FOOTER_PROFILE_BACK
                )
            if self._profiles_name_prompt is not None:
                return (
                    _FOOTER_PROFILE_NAME_RENAME
                    if self._profiles_name_prompt["mode"] == "rename"
                    else _FOOTER_PROFILE_NAME_CREATE
                )
            if self._profiles_on_none_row():
                return (
                    _FOOTER_PROFILES_NONE
                    if self._config_profile()
                    else _FOOTER_PROFILES_NONE_EMPTY
                )
            if self._profiles_on_add_row():
                # На строке-действии общий футер врал бы всеми клавишами объектов
                # (открыть/Space/r/Del на ней не работают) — канон «недоступное
                # не обещаем», тот же приём, что у строки уровня.
                return _FOOTER_PROFILES_ADD
            return _FOOTERS[_PROFILES]
        return _FOOTERS.get(self._view, _FOOTER_READONLY)

    def _footer_text(self) -> str:
        # Клавиша прокрутки называется, только пока есть что прокручивать —
        # то же правило «мёртвые клавиши не рекламируются», что и на экране
        # изменений. На высоком окне подсказки о ней нет вовсе.
        base = self._footer_base()
        return (
            base + FOOTER_SCROLL
            if body_scrollable(self, _SEL_SET_SCROLL)
            else base
        )

    def on_resize(self, event: events.Resize) -> None:
        # Потолок рамки зависит только от высоты окна и хрома — пересчитывается на
        # ресайз, а не на каждый кадр секции (см. form_frame).
        cap_frame_height(self, _SEL_SET_SCROLL)
        if self._modes_picker is not None:
            self._cap_model_picker(self._modes_picker)
        # Влезет ли содержимое, видно только ПОСЛЕ раскладки — от этого зависит,
        # называть ли клавишу прокрутки.
        self.call_after_refresh(self._refresh_footer)
        self.call_after_refresh(self._refresh_modes_rows)

    def _profile_footer(self, ed) -> str:
        """Футер под-вида профиля: строку даёт редактор (она позиционная), а
        обещание `v` дописывает ЭКРАН — просмотрщик его, и гасит обещание его же
        настройка (§11a). Редактор про файловый просмотр не знает ничего.

        Обещаем ровно там, где клавиша работает: под висящим выбором (форма,
        пикер, вопрос) её нет — у тех состояний и варианта с `v` не заведено."""
        footer = ed.footer_text()
        if not (ed.showing_list and self._file_view_settings().enabled):
            return footer
        from gc_tui.editors.models_section import FOOTERS_WITH_VIEW

        return FOOTERS_WITH_VIEW.get(footer, footer)

    def _file_view_settings(self):
        """Настройки встроенного просмотрщика — тем же фабричным методом и из того
        же ключа конфига, что у `/view` главного экрана: вторая сборка разошлась бы
        с ней на первой же правке схемы."""
        from gc_tui.services.file_view import FileViewSettings

        return FileViewSettings.from_mapping(
            self._cfg_settings().tui.file_view.model_dump()
        )

    def _cfg_settings(self):
        """Типизированный вид базы экрана (`AppConfig`) — для витрин. Правка и
        запись config.yml по-прежнему идут сырыми документами."""
        return settings_of(self._settings)

    def _config_profile(self):
        """Профиль, под которым собрана база экрана (указатель файлов либо флаг
        запуска `--profile`); None — без профиля. Сессионный выбор `/profile`
        сюда не входит — экран правит файлы, а не сессию."""
        return runtime_of(self._settings).active_profile or None

    def _pull_base_from_app(self) -> None:
        """Забрать перечитанную базу окна у приложения (после `_on_apply`).
        Приложение без базы (экран собран вне TUI — юнит-тесты витрин) — база
        экрана остаётся прежней."""
        getter = getattr(self.app, "base_settings", None)
        if callable(getter):
            self._settings = getter()

    def _connection_editor(self):
        """Свежесмонтированный редактор секции «Подключение».

        ПОСЛЕДНИЙ матч и NoMatches на пустом — тот же канон, что у `_voice_editor`:
        после `_render_view` старый редактор ещё в дереве (`remove()` отложенный), а
        `query_one` на двух матчах не падает, а молча отдаёт ПЕРВЫЙ, обречённый. Здесь
        это стоило бы дорого: через этот же редактор идёт сохранение секции, то есть
        в конфиг уехали бы значения прошлого рендера (находка 2026-08-07)."""
        from gc_tui.editors.connection import ConnectionEditor

        editors = list(self.query(ConnectionEditor))
        if not editors:
            raise NoMatches("ConnectionEditor не смонтирован")
        return editors[-1]

    def _presets_open(self) -> bool:
        try:
            return self._connection_editor().presets_open
        except NoMatches:
            return False

    def _presets_have_selection(self) -> bool:
        try:
            return self._connection_editor().presets_have_selection
        except NoMatches:
            return False

    def _connection_on_url(self) -> bool:
        """Курсор ленты «Подключения» — на поле адреса. Редактора ещё нет (самый первый
        кадр секции) — курсор по умолчанию там (`ConnectionEditor._idx` стартует с 0 =
        адрес), поэтому «не смонтирован» тоже читаем как «да»."""
        try:
            return self._connection_editor().on_url_field
        except NoMatches:
            return True

    def _voice_footer_text(self) -> str:
        """Футер голосовой секции — про место под курсором: обещать клавишу, которая
        здесь ничего не делает, значит врать (Ctrl+Space в локальной панели — no-op).

        Панель под курсором (`cloud`) читается ДО ветки «список раскрыт»: у языкового
        поля и у «без совпадений» список общий для local/cloud (модели и URL — только
        у облака), а проба есть лишь в облаке (§2.5 спеки 2026-08-06) — без этого
        порядка локальная панель получила бы чужое обещание Ctrl+Space."""
        field = self._voice_cursor_field()
        ctrl = self._voice_list_ctrl()
        cloud = self._voice_pane == "cloud"
        if (
            ctrl is not None and ctrl.is_open
        ):  # раскрытый список забирает клавиши себе
            if not ctrl.has_selection:
                return (
                    _VOICE_FOOTER_LIST_NO_MATCH_CLOUD
                    if cloud
                    else _VOICE_FOOTER_LIST_NO_MATCH_LOCAL
                )
            if field == "model":
                return _VOICE_FOOTER_MODEL_LIST
            if field == "url":
                return _VOICE_FOOTER_URL_LIST
            return (
                _VOICE_FOOTER_LANG_LIST_CLOUD
                if cloud
                else _VOICE_FOOTER_LANG_LIST_LOCAL
            )
        if cloud:
            if field in _VOICE_LIST_FIELDS:
                # Модель — сетевой список: свёрнутое состояние обязано звать Alt+↓ тем
                # же глаголом, что и раскрытое («перечитать список»), иначе оно
                # разойдётся с хинтом под рамкой поля (`closed_hint`, находка C).
                return (
                    _VOICE_FOOTER_CLOUD_LIST_MODEL
                    if field == "model"
                    else _VOICE_FOOTER_CLOUD_LIST
                )
            if field == "token" and self._voice_token_is_mask():
                # Под маской — сохранённый секрет, и Ctrl+U его стирает: клавиша,
                # промолчавшая в футере, для человека не существует.
                return _VOICE_FOOTER_CLOUD_TOKEN
            # Курсор в вариантах (`field is None`) и курсор в поле без списка (осталось
            # одно — «Токен», URL ушёл в список выше) отличаются только направлением
            # Tab — им и раздвоен футер облачной панели.
            return (
                _VOICE_FOOTER_CLOUD_FIELD
                if field is not None
                else _VOICE_FOOTER_CLOUD
            )
        s = self._voice_status
        if s is None or not s.installed:
            return _VOICE_FOOTER_LOCAL_FRESH  # удалять нечего — Del не обещаем
        if self._voice_cursor_group() == _VOICE_GROUP_FIELDS:
            return (
                _VOICE_FOOTER_LOCAL_LANG  # единственное поле панели — «Язык»
            )
        if s.available_version:
            return _VOICE_FOOTER_LOCAL_UPDATE
        return _VOICE_FOOTER_LOCAL

    def _connection_can_clear_token(self) -> bool:
        """Курсор на токене и есть что стирать — футер обещает Ctrl+U. Редактора
        нет — обещать нечего (курсор стартует на адресе)."""
        try:
            return self._connection_editor().on_token_field_with_saved()
        except NoMatches:
            return False

    def on_connection_editor_ready(self, event) -> None:
        """Редактор адреса домонтировался: заполнил поле и решил, раскрывать ли
        пресет-лист (§2.1). Только теперь футер знает, чьи сейчас клавиши, а
        dirty-маркер видит настоящий адрес: оба расчёта `_render_view` (сразу после
        монтажа тела и повтором `call_after_refresh`) идут РАНЬШЕ `on_mount` редактора,
        когда поле ещё пусто и секция читается «изменено»."""
        event.stop()
        self._refresh_footer()
        self._update_dirty()

    def _refresh_footer(self) -> None:
        """Футер меняется вместе с курсором — обновляем на месте (ре-рендер секции для
        этого не нужен и стёр бы черновик полей)."""
        try:
            self.query_one(_SEL_SET_FOOTER, Static).update(self._footer_text())
        except NoMatches:
            pass

    def on_mount(self) -> None:
        # Вердикт стартового авто-чека (app) — общий на сессию: хаб показывает статус сразу,
        # а секция не ходит в сеть второй раз (Enter на «Проверить обновления» — форсит).
        self._update_check_result = getattr(
            self.app, "_update_check_result", None
        )
        endpoint, _a, _v, _m, _p = self._probe_args_from_config()
        self._probe_in_flight = bool(
            endpoint
        )  # первый кадр меню сразу с …, без — → ✓ мелькания
        self._render_view()
        # Каждое открытие секции начинается сверху. `VerticalScroll` сохраняет
        # собственное смещение между пересборками тела, поэтому без явного сброса
        # ползунок мог оставаться внизу при уже отрисованном верхе формы.
        self.call_after_refresh(
            lambda: self.query_one(
                _SEL_SET_SCROLL, VerticalScroll
            ).scroll_home(animate=False)
        )
        self._start_config_probe()
        # Живость порта батарейки — первый замер сразу (глиф меню судит по ней) и
        # тихий интервал: пока открыта голосовая секция, статус «запущен/не запущен»
        # обязан оставаться живым без единого сокета на UI-потоке.
        self._kick_voice_alive()
        self.set_interval(3.0, self._voice_alive_tick)
        self.call_after_refresh(cap_frame_height, self, _SEL_SET_SCROLL)

    # --- сырые значения полей (файл — источник для редакторов и сверки «не изменено») ---
    def _read_raw(self) -> dict:
        """Значения полей визарда ИЗ ФАЙЛА конфига, как записаны (без резолва ${ENV:VAR}).

        В настройках окна они уже резолвнуты (config_modules.env_interpolation), и показать
        или записать их обратно в файл значит подменить ссылку на переменную её значением —
        секрет открытым текстом в файле, который коммитят. Экран правит ФАЙЛ, значит и
        читать должен файл; резолвнутый конфиг остаётся источником только для probe-запросов
        (они проверяют действующий credential)."""
        from gc_core import config_raw
        from gc_core.onboarding import FIELD_ANCHOR_PATHS

        if not self._config_path:
            return {}
        values = config_raw.read_values(
            self._config_path, FIELD_ANCHOR_PATHS.values()
        )
        return {
            leaf: values[dotted] for leaf, dotted in FIELD_ANCHOR_PATHS.items()
        }

    def _acting_profile(self):
        """Профиль, под которым СОБРАН конфиг этого окна, — для `config_origin.origins`
        (`""` — «без профиля», None — судить нечем, пусть спрашивает указатель у файлов).

        Ответ один на всё приложение — `runtime.active_profile` из
        `effective_settings()`: загрузчик кладёт туда ДЕЙСТВУЮЩЕЕ имя (флаг запуска
        `--profile` и сессионный
        выбор `/profile` включительно), а не указатель из файла; своей редакции
        вопроса «какой профиль действует» здесь не заводим — она разошлась бы с
        футером и `/config` на первой же правке.

        Витрины происхождения обязаны судить тот стек, на котором окно ЗАПУЩЕНО:
        под `gc-ua --profile ""` (путь отхода, который советует сломанный конфиг)
        диск по-прежнему говорит `profile: work` — и ответ по нему прятал бы точку
        починки за «перекрыто профилем work», профилем, которого в этом запуске нет.

        Приложения рядом нет (экран собран вне запущенного TUI — так его зовут
        юнит-тесты витрин) — None: судить не по чему, остаётся ответ по файлам."""
        try:
            profile = self.app.effective_settings().runtime.active_profile
        except Exception:
            return None  # диагностика не обязана ронять экран
        return profile if isinstance(profile, str) else None

    def _read_origins(self) -> dict:
        """Происхождение полей визарда: что в базовом файле, чем перекрыто, что действует.

        загрузчик кладёт поверх базового файла machine-managed и проектный оверлеи —
        экран правит базовый файл, и без этого молчал бы о том, что правка не подействует."""
        from gc_core.config_origin import origins
        from gc_core.onboarding import FIELD_ANCHOR_PATHS

        if not self._config_path:
            return {}
        by_dotted = origins(
            self._config_path,
            FIELD_ANCHOR_PATHS.values(),
            profile_override=self._acting_profile(),
        )
        return {
            leaf: by_dotted[dotted]
            for leaf, dotted in FIELD_ANCHOR_PATHS.items()
        }

    def _origin(self, key: str):
        """Origin поля или None (путь конфига не задан)."""
        return self._origins.get(key)

    def _registry_origin(self):
        """Происхождение реестра `models` — тем же `origins`, что и поля визарда, но
        свежим чтением на каждый проход UI, а не из `_origins`: спрашивает его секция
        «Модели» на каждую сборку тела, в том числе сразу после СВОЕЙ записи, когда
        снимки экрана ещё не перечитаны. Судить по ним значило бы видеть снятую
        запись живой — и рисовать подсказку о слое сверху вместо легаси-редактора.

        Ответ живёт проход — в том же кэше, что и дисковые сводки профилей
        (`_profiles_disk_cache`): за один проход его спрашивают дважды
        (`_legacy_default()` и тело секции), а стоит он stat managed-слоя, резолва
        указателя, обхода корней профилей и разбора каждого файла стека. Свежесть та
        же: кэш сбрасывается там же, где и соседние чтения, — на входе в
        `_render_view` и сразу после записи секции, ДО того как та перечитает себя
        (`_models_changed` перед `_reload`: перечитка спрашивает витрину первым же
        делом, и сброс после неё оставлял бы на экране ответ о состоянии ДО
        записи)."""
        from gc_core.config_origin import origins

        if not self._config_path:
            return None
        cache = self._profiles_disk_cache
        if "registry_origin" not in cache:
            cache["registry_origin"] = origins(
                self._config_path,
                ["models"],
                profile_override=self._acting_profile(),
            )["models"]
        return cache["registry_origin"]

    def _raw_value(self, key: str) -> str:
        """Сырое значение поля строкой; сентинел шаблона / отсутствие → "" (не задано)."""
        from gc_core.onboarding import (
            MODEL_SENTINEL,
            TOKEN_SENTINEL,
            URL_SENTINEL,
        )

        v = self._raw.get(key)
        if not isinstance(v, str) or v in (
            URL_SENTINEL,
            TOKEN_SENTINEL,
            MODEL_SENTINEL,
        ):
            return ""
        return v

    # --- значения для меню (правый столбец, dim) ---
    def _row_value(self, i: int) -> Text:
        from gc_core.onboarding import (
            TOKEN_SENTINEL,
            display_secret,
            display_value,
            env_ref_status,
        )

        v = Text()
        if i == _CONNECTION:
            url = self._raw_value("completions_url")
            auth = self._raw.get("Authorization")
            host = display_value(url) if url else "—"
            if not auth:
                mode = "без авторизации"
            elif auth == TOKEN_SENTINEL:
                mode = "—"
            elif env_ref_status(auth):
                mode = display_secret(
                    auth
                )  # ссылка + статус (переменной нет / подставилась)
            else:
                mode = "Bearer"
            proxy = self._raw_value("proxy")
            proxy_part = " · прокси" if proxy else ""
            v.append(f"{host} · {mode}{proxy_part}", style=DIM)
        elif i == _MODELS:
            # Реестр есть — превью про него («N в реестре · default …»); реестра нет
            # — прежнее превью легаси-поля. Оба факта — ФАЙЛОВЫЕ (решение 6).
            from gc_tui.editors.models_section import registry_preview

            document = self._target_document()
            if document.get("models"):
                v.append(registry_preview(document), style=DIM)
            else:
                model = self._raw_value("model")
                v.append(display_value(model) if model else "—", style=DIM)
        elif i == _PROFILES:
            name = self._config_profile()
            v.append(name if name else "не выбран", style=DIM)
        elif i == _MCP:
            try:
                from recommended_mcp.runtime import cli as mcp_mod

                n = len(mcp_mod.wired_names())
            except Exception:
                n = 0
            if n:
                v.append(f"{n} подключено", style=OK)
            else:
                v.append("нет подключённых", style=DIM)
        elif i == _VOICE:
            s = self._voice_snapshot()
            if s.mode == "local":
                val = f"локальный сервис · {s.model or '—'}"
            elif s.mode == "cloud":
                # Сам адрес и модель, зеркально локальной строке выше («что стоит»,
                # а не «какой режим»): родовое «свой endpoint» не говорило ничего,
                # а строка «Подключение» рядом уже показывает полный URL — один язык
                # меню. Обрезает штатный ellipsis строки.
                raw = self._voice_raw_cloud() or {}
                url = str(raw.get("url") or "").strip()
                model = str(raw.get("model") or "").strip()
                val = f"{url} · {model or '—'}" if url else "свой endpoint"
            else:
                val = "выключен"
            v.append(val, style=DIM)
        elif i == _MODES:
            from gc_tui.toggles import TOGGLES, modes_section_names

            # Превью меню — детальные пары (preset-тоггл rigor показывается витриной
            # в самой секции, не дублируется в одну строку меню). Скоуп — СОСТАВ
            # секции: превью обязано перечислять то же, что человек увидит внутри,
            # и спрашивает его там же, где рендер (`modes_section_names`).
            pairs = [
                f"{TOGGLES[name].label}: {self._modes_value_plain(name)}"
                for name in modes_section_names()
                if not TOGGLES[name].preset_keys
            ]
            v.append(
                fit_mode_pairs(pairs, self._modes_preview_width()), style=DIM
            )
        elif i == _LOOK:
            v.append(f"{_THEME_LABEL}: {self._theme_name()}", style=DIM)
        else:
            v.append(self._version_text().split("·", 1)[-1].strip(), style=DIM)
        return v

    def _modes_preview_width(self) -> int:
        """Ширина региона `.set-rowtext` строки «Режимы работы» — граница усечения
        превью целыми парами (fit_mode_pairs). До монтирования этой строки меню
        (первый рендер) региона ещё нет — 0, что трактуется как «без усечения»."""
        try:
            rows = list(self.query(_SEL_SET_MENUROW))
            return rows[_MODES].query_one(".set-rowtext", Static).region.width
        except (NoMatches, IndexError):
            return 0

    def _menu_rowtext(self, i: int, selected: bool) -> Text:
        """Левая часть строки меню: маркер + имя секции + значение (глиф — отдельно,
        в правой колонке .set-glyph; длинное значение обрезает layout по 1fr)."""
        t = Text()
        t.append("▸ " if selected else "  ", style=ACCENT)
        t.append(f"{SECTIONS[i]:<18}", style=INK)
        t.append("  ")
        t.append_text(self._row_value(i))
        return t

    def _mount_menu_row(self, frame: Vertical, i: int) -> None:
        cls = "set-menurow set-dyn" + (
            _CLS_SELECTED if i == self._menu_idx else ""
        )
        frame.mount(
            Horizontal(
                Static(
                    self._menu_rowtext(i, i == self._menu_idx),
                    classes="set-rowtext",
                ),
                Static(self._menu_glyph_text(i), classes="set-glyph"),
                classes=cls,
            )
        )

    def _menu_glyph_text(self, i: int) -> Text:
        """Text глифа строки: анимированная волна в полёте probe (редакторные секции,
        _EDITOR_SECTIONS), иначе одиночный глиф ✓/!/— из _menu_glyph."""
        if i in _EDITOR_SECTIONS and self._probe_in_flight:
            return self._wave_glyph()
        glyph, gstyle = self._menu_glyph(i)
        return Text(glyph, style=gstyle)

    def _wave_glyph(self) -> Text:
        """Бегущая волна загрузки: три точки `·`, подсвеченная (teal) перебегает
        по WAVE_SEQ (`·` teal → следующая → …), остальные — dim."""
        return wave_glyph(self._wave_i)

    def _start_wave(self) -> None:
        if self._wave_timer is None:
            self._wave_timer = self.set_interval(
                WAVE_INTERVAL, self._wave_tick
            )

    def _stop_wave(self) -> None:
        if self._wave_timer is not None:
            self._wave_timer.stop()
            self._wave_timer = None

    def _wave_tick(self) -> None:
        if not self.is_mounted:
            self._stop_wave()
            return
        self._wave_i = (self._wave_i + 1) % len(WAVE_SEQ)
        if self._view == "menu" and self._probe_in_flight:
            self._refresh_wave_glyphs()  # только глифы (лёгкий кадр, без ре-рендера строк)
        elif self._view == _VOICE and self._voice_probe_in_flight:
            self._set_probe_line(
                self._voice_probe_line()
            )  # та же волна в probe-строке голоса
        else:
            self._stop_wave()

    def _refresh_wave_glyphs(self) -> None:
        for i, row in enumerate(self.query(_SEL_SET_MENUROW)):
            if i not in _EDITOR_SECTIONS:
                continue
            try:
                row.query_one(".set-glyph", Static).update(
                    self._menu_glyph_text(i)
                )
            except NoMatches:
                continue

    def on_unmount(self) -> None:
        self._stop_wave()

    def refresh_update_glyph(self) -> None:
        """Стартовый авто-чек прилетел (app зовёт с UI-потока) — подтянуть его вердикт и
        перерисовать строку меню. Свой вердикт (ручная проверка) не затираем."""
        if self._update_check_result is None:
            self._update_check_result = getattr(
                self.app, "_update_check_result", None
            )
        if self._view == "menu":
            self._refresh_menu_rows()

    def _target_snapshot(self) -> tuple[str, dict]:
        """(текст, документ) цели записи за ОДНО чтение на проход отрисовки меню.

        Строку «Модели» спрашивают минимум дважды за кадр (превью и глиф), а глифу
        нужен ещё и текст — для валидации. Без снимка это четыре чтения файла и
        разбор YAML заново на каждое ↑/↓ в меню. Кэш прохода — тот же приём, что у
        `_profiles_disk_cache` секции «Профили», и сбрасывается там же, где рисуется
        меню: правка файла извне видна с первого же кадра.

        ЧИТАЕТСЯ файл на каждом проходе (0.1 мс — за этим свежесть и держится), а вот
        РАЗБИРАЕТСЯ только новый текст: `parse_document` конфига размером с шаблон
        стоит 10 мс, и платить их за каждое ↑/↓ ради того же самого документа не за
        что. Ключ — сам текст, поэтому «тот же документ» здесь значит буквально то же
        содержимое, а не «файл вроде бы не трогали»."""
        if self._menu_pass_snapshot is None:
            from gc_core import config_raw
            from gc_core.utils.user_text import read_user_text_or_none

            text = ""
            if self._config_path:
                # Читается на КАЖДОМ проходе отрисовки меню: отказ здесь роняет весь
                # экран настроек — ровно там, куда человек и приходит чинить конфиг,
                # сохранённый в чужой кодировке. Пустой текст = «показать меню без
                # превью», и это единственный исход, который его не запирает.
                text = read_user_text_or_none(self._config_path) or ""
            if self._parsed_target is None or self._parsed_target[0] != text:
                self._parsed_target = (text, config_raw.parse_document(text))
            self._menu_pass_snapshot = self._parsed_target
        return self._menu_pass_snapshot

    def _target_document(self) -> dict:
        """Сырой документ цели записи (базовый config.yml) — ФАЙЛОВОЕ состояние секции
        «Модели» (решение 6 волны B): реестр редактируется в файле, а не в резолве."""
        return self._target_snapshot()[1]

    def _config_stack_texts(self) -> tuple | None:
        """Содержимое файлов, из которых соберётся стек следующего старта:
        `((путь, текст|None), …)` либо None, когда состав выяснить не удалось.

        Состав называет `config.profile_mtime_set` — тот же набор, по которому
        приложение решает, устарел ли конфиг. Берётся из него именно СОСТАВ, а не
        мтаймы: снимок нужен ключом кэша вердикта, а устаревший глиф хуже медленных
        стрелок — разрешения `st_mtime` на Windows для этого не хватает, содержимое же
        отвечает точно и дёшево (≈1 мс против ≈30 мс самой валидации). Указатель —
        общим резолвом `resolve_profile_pointer` (свежий мерж база → managed → project,
        порядок `_merge_layers`), а не сырым документом базы и не базой окна
        порознь: раскладка «указатель назначен проектным `.gc/config.yml`, база о нём
        молчит» первому источнику не по зубам, а раскладка «указатель есть в обоих, и
        они расходятся» — второму (база окна может быть собрана в обход резолва,
        как у синтетических сборок экрана; а победить при конфликте обязан проект,
        а не выигравший по случайности источник)."""
        if not self._config_path:
            return None
        from gc_core.config import profile_mtime_set, resolve_profile_pointer
        from gc_core.utils.user_text import read_user_text_or_none

        try:
            pointer = resolve_profile_pointer(self._config_path) or None
            paths = profile_mtime_set(self._config_path, pointer)
        except OSError:
            return None
        # None вместо текста — законная позиция снимка: «файл в наборе есть, но не
        # читается». Отказ вместо неё ронял ВЕСЬ экран настроек на рендере меню
        # (ключ кэша считается для глифа «Модели»), ровно там, куда человек и
        # приходит чинить такой профиль.
        return tuple((path, read_user_text_or_none(path)) for path in paths)

    def _models_verdict(self, text: str) -> str | None:
        """Вердикт валидации секций моделей: `"schema"` — файл не проходит схему (чужая
        ошибка, не реестра), `"sections"` — сломаны ссылки слотов/привязок, None — чисто.

        Судится СТЕК, который соберёт следующий старт (`validate_config_text_with_sections`
        с `base_path`), а не один файл: реестр кросс-слоен по устройству — слот профиля
        вправе смотреть на модель базы, — и однофайловый вердикт врёт в обе стороны:
        красит «!» рабочую раскладку «модели в основном конфиге, уровни в профиле» и
        молчит о слоте активного профиля, смотрящем в удалённую запись.

        Считается не чаще, чем меняется хоть один файл стека: полный прогон YAML+схемы
        стоит ≈30 мс на конфиге размером с шаблон, а строку хаба перерисовывает КАЖДОЕ
        ↑/↓ в меню. Сбрасывать кэш некому и незачем: ключ — само содержимое файлов
        (и правимый текст в том числе), и любая запись — своя ли, чужая ли — меняет его
        вместе с вердиктом. Состав стека выяснить не удалось — не кэшируем вовсе."""
        stack = self._config_stack_texts()
        key = None if stack is None else (text, stack)
        cached = self._models_verdict_cache
        if key is not None and cached is not None and cached[0] == key:
            return cached[1]
        from gc_core.config import (
            _app_config_from_text,
            validate_config_text_with_sections,
        )

        verdict = None
        try:
            _app_config_from_text(
                text
            )  # фаза 1 — про САМ файл: чужой слой не при чём
        except ValueError:
            verdict = "schema"
        if verdict is None:
            try:
                validate_config_text_with_sections(
                    text, base_path=self._config_path
                )
            except ValueError:
                verdict = "sections"
        self._models_verdict_cache = None if key is None else (key, verdict)
        return verdict

    def _models_glyph(self) -> tuple[str, str]:
        """Вердикт строки «Модели» — про ФАЙЛЫ, а не про сеть (потому секция и вышла из
        probe-волны). Реестр непуст: сломанные ссылки слотов/привязок → `!`; реестр,
        в котором ни одна запись не резолвит адрес, → `—` (валиден, но не работает —
        та же дыра схемы, о которой секция говорит строкой); иначе `✓`. Реестра нет —
        прежний вердикт легаси-поля (модель есть в списке сервера).

        Чужая ошибка конфига (опечатка в посторонней секции) валит фазу 1 и красила бы
        `!` именно здесь — за неё эта строка не отвечает: `—`, «вердикта нет»."""
        text, document = self._target_snapshot()
        if document.get("models"):
            from gc_tui.editors.models_section import (
                registry_resolves_any_address,
            )

            verdict = self._models_verdict(text)
            if verdict == "schema":
                return (
                    "—",
                    DEEP_DIM,
                )  # файл не проходит схему — но не из-за реестра
            if verdict == "sections":
                return "!", ERR  # ссылки слотов/привязок сломаны — это наше
            if not registry_resolves_any_address(
                document, self._probe_args_from_config()[0]
            ):
                return (
                    "—",
                    DEEP_DIM,
                )  # адреса нет ни у записей, ни в «Подключении»
            return "✓", OK
        r = self._probe_config
        model = self._cfg_settings().agent.model or ""
        if not model or r is None or r.models is None:
            return "—", DEEP_DIM
        ids = [m.id for m in r.models]
        return (
            ("✓", OK) if model in ids else ("—", DEEP_DIM)
        )  # не в списке → —, не !

    def _menu_glyph(self, i: int) -> tuple[str, str]:
        """(глиф, style) для строки меню. … в полёте; ✓/!/— по §2 контракта.
        Глиф — вердикт probe о ДЕЙСТВУЮЩИХ (резолвнутых) значениях: он про то, работает
        ли конфиг, а не про то, что записано в файле (это показывает столбец значения)."""
        if i in _EDITOR_SECTIONS:
            if self._probe_in_flight:
                return "…", DEEP_DIM
            r = self._probe_config
            if i == _CONNECTION:
                from gc_core.onboarding import TOKEN_SENTINEL

                endpoint, _a, _v, _m, _p = self._probe_args_from_config()
                if not endpoint:
                    return "—", DEEP_DIM
                if r is None:
                    return "—", DEEP_DIM
                # Красный — только про подключение. Содержательный отказ про модель
                # (живой GigaCode: 404 «проверьте адрес и имя модели») сюда не
                # попадает: имя модели — забота своей строки, у неё свой глиф.
                if r.offline:
                    return "!", ERR  # сервер недоступен СЕЙЧАС
                if r.http_status is None:
                    # Ответа нет, но и до сервера мы не доходили: сорвалась сама
                    # проверка (фолбэк `_start_config_probe`). Утверждать за сервер
                    # нечего — строка секции про этот же результат пишет «не удалось
                    # проверить», и хаб не имеет права быть категоричнее её.
                    return "—", DEEP_DIM
                auth = self._raw.get("Authorization")
                if auth == TOKEN_SENTINEL:
                    return (
                        "—",
                        DEEP_DIM,
                    )  # токен не задан (шаблон) — нечем подтвердить
                # Вердикт о токене читаем через общий словарь смыслов — тот же, что у
                # строки секции и у реплики после сохранения: три собственных прочтения
                # одного результата уже разъезжались (хаб верил публичному /models).
                # Токен есть, а вердикта о нём нет — молчим («—»), а не гадаем.
                from gc_tui.services.probe_presentation import token_accepted

                accepted = token_accepted(r, has_token=bool(auth))
                if accepted is None:
                    return "—", DEEP_DIM
                return ("✓", OK) if accepted else ("!", ERR)
        if i == _MODELS:
            return self._models_glyph()
        if i == _MCP:
            try:
                from recommended_mcp.runtime import cli as mcp_mod

                return ("✓", OK) if mcp_mod.wired_names() else ("—", DEEP_DIM)
            except Exception:
                return "—", DEEP_DIM
        if i == _VOICE:
            s = self._voice_snapshot()
            if s.mode == "local" and s.service_alive:
                return "✓", OK
            if s.mode == "cloud":
                return "✓", OK
            return "—", DEEP_DIM
        if i == _UPDATE:
            r = self._update_check_result
            if r is not None:
                if r.status == "update":
                    return "↑", ACCENT
                if r.status == "current":
                    return "✓", OK
                return "—", DEEP_DIM  # unknown/не удалось — нет вердикта
            if getattr(self.app, "_update_available", False):
                return "↑", ACCENT  # вердикта сессии нет — персист-флаг
            if self._update_checking or getattr(
                self.app, "_update_checking", False
            ):
                return (
                    "…",
                    DEEP_DIM,
                )  # стартовый чек в полёте — вердикт вот-вот будет
            return "—", DEEP_DIM
        if i == _PROFILES:
            return ("✓", OK) if self._config_profile() else ("—", DEEP_DIM)
        return "—", DEEP_DIM

    # --- рендер (полная замена тела фрейма, кроме персистентного #set-title) ---
    def _render_view(self) -> None:
        self._profiles_disk_cache = {}  # новый кадр — дисковые данные экрана перечитываются
        self._menu_pass_snapshot = None  # и цель записи секции «Модели» — тоже
        if self._report_section != self._view:
            # Отчёт живёт в СВОЕЙ секции: сменили — закрылся. А вот перерисовка
            # той же секции (её вызывают и стрелки — «Обновление», «Профили»,
            # «Режимы») его не трогает: он состояние экрана, и ниже монтируется
            # заново вместе с телом. Иначе «живёт до явного Esc» не держалось бы
            # ровно на самых частых клавишах (находка ревью хвоста).
            self._report_text = None
            self._report_section = None
        self.query_one("#header-left", Static).update(self._title_text())
        self.query_one(_SEL_SET_FOOTER, Static).update(self._footer_text())
        # Влезет ли содержимое, видно только ПОСЛЕ раскладки — а от этого
        # зависит, называть ли клавишу прокрутки.
        self.call_after_refresh(self._refresh_footer)
        frame = self.query_one(_SEL_SET_FRAME, Vertical)
        title = self.query_one("#set-title", Static)
        # Снять фокус ДО remove: detached focused-виджет рвёт последующую доставку
        # клавиш (тот же баг и то же лекарство, что у OnboardingScreen._mount_step_body —
        # секции без своего поля (Режимы/MCP/Обновление) не перехватывают фокус заново,
        # и PgUp/PgDn после них переставали находить секцию). Секции со своим Input
        # (Адрес/Модель/облачные поля голоса) перефокусируют себя сами при монтировании.
        self.set_focus(None)
        # Убираем ВСЁ динамическое, а не только .set-dyn: корни редакторов несут
        # класс onb-dyn (общий с онбордингом), поэтому class-фильтр их пропускал —
        # редактор оставался при Esc (мусор + меню внизу). Чистим по факту детей.
        for w in list(frame.children):
            if w is not title:
                w.remove()
        self._modes_picker = None  # список жил в рамке и снят вместе с ней
        if self._view == "menu":
            self._set_title("Настройки")
            for i in range(len(SECTIONS)):
                self._mount_menu_row(frame, i)
            # Carry-fix (ревью T2, Low): строки монтируются в ЭТОМ же такте — региона
            # у ещё не смонтированной строки i при подсчёте _row_value(i) нет (см.
            # _modes_preview_width), превью «Режимы работы» не усекается на первом
            # кадре. call_after_refresh форсит второй проход ПОСЛЕ layout, когда
            # регионы строк уже посчитаны — тот же паттерн, что и scroll_visible ниже.
            self.call_after_refresh(self._refresh_menu_rows)
        else:
            self._mount_section_body(frame, self._view)
            self._update_dirty()
            if self._view == _PROFILES and self._profiles_open is not None:
                self.call_after_refresh(self._refresh_profile_overlay)
            elif self._view == _PROFILES:
                self.call_after_refresh(self._refresh_profiles_hint)
        if self._report_text is not None:
            self._mount_report(
                self._report_text
            )  # пережил перерисовку своей секции
        # Футер — про тело, поэтому считается ПОСЛЕ него (канон мастера
        # `_mount_step_body`): состав тела задаёт и состав обещаний.
        self._refresh_footer()
        # И подводка — тоже про тело и тоже после него: секция бывает длиннее вьюпорта
        # рамки, и без неё экран отвечал на курсор, которого не видно, и просил Enter
        # на вопрос, которого не видно. Одна точка на весь экран (см. form_frame):
        # ярусы приоритета родовые, перечня конкретных баннеров здесь нет.
        reveal_focus(self)
        if self._view == _CONNECTION:
            # И ещё раз, когда дерево устоялось: `ConnectionEditor` раскрывает пресет-лист
            # в СВОЁМ on_mount — это уже после вызова выше, и первый кадр секции обещал бы
            # «Enter — сохранить» поверх раскрытого списка. Только этой секции: отложенный
            # пересчёт «вслепую» опасен там, где содержимое приезжает из сети (голос,
            # список моделей) — он лёг бы между раскрытием списка и приездом строк и
            # оставил бы футер на «совпадений нет» до следующего события.
            self.call_after_refresh(self._refresh_footer)

    # --- dirty-маркер заголовка (спека v2 §3.1, Task 7) ---
    def _section_title(self) -> str:
        """Имя секции в заголовке рамки — с хвостом под-вида, если он открыт: внутри
        профиля тело секции чужое (редактор форм), и заголовок обязан сказать, ЧЕЙ
        файл правится. Спрашивают его два места (`_update_dirty` и `_title_note`) —
        значит считает одно.

        У секции «Модели» заголовка внутри рамки НЕТ: её имя уже стоит в шапке экрана
        («Настройки · Модели»), а тело подписывает свои блоки само (`models_section.
        block_caption`) — третий повтор того же слова стоил бы двух строк экрана. В
        под-виде открытого профиля заголовок остаётся: он называет не секцию, а файл."""
        name = SECTIONS[self._view]
        if self._view == _PROFILES and self._profiles_open is not None:
            return f"{name} · профиль {self._profiles_open}"
        return "" if self._view == _MODELS else name

    def _set_title(self, content) -> None:
        """Единственная точка записи в `#set-title`. Пустой заголовок — это не пустая
        строка на экране, а её ОТСУТСТВИЕ (`display: False`): секция без заголовка не
        должна платить за него ни строкой текста, ни отступом под ним. Обратный переход
        — оттуда же: слот заголовка занимают и кратковременные реплики (`_title_note`),
        и они обязаны быть видны в любой секции."""
        try:
            title = self.query_one("#set-title", Static)
        except NoMatches:
            return
        title.update(content)
        title.display = bool(
            content.plain if isinstance(content, Text) else content
        )

    def _update_dirty(self) -> None:
        """Обновляет #set-title: имя секции (+ dirty-маркер « изменено» для редакторных
        секций, _EDITOR_SECTIONS)."""
        if self._view == "menu":
            return
        name = self._section_title()
        if self._view in _EDITOR_SECTIONS and self._is_dirty(self._view):
            t = Text(name)
            t.append("  изменено", style=WARN)
            self._set_title(t)
        else:
            self._set_title(name)

    def _title_note(self, text: str) -> None:
        """Кратковременная спокойная реплика в слоте заголовка (там же, где «изменено»).
        Гаснет через _TITLE_NOTE_SEC; повторный вызов перезапускает таймер."""
        if self._view == "menu":
            return
        t = Text(self._section_title())
        t.append("  " + text, style=DEEP_DIM)
        self._set_title(t)
        if self._note_timer is not None:
            self._note_timer.stop()
        self._note_timer = self.set_timer(
            _TITLE_NOTE_SEC, self._clear_title_note
        )

    def _clear_title_note(self) -> None:
        self._note_timer = None
        self._update_dirty()  # вернуть истинное состояние (имя / «изменено»)

    def _is_dirty(self, section: int) -> bool:
        """Значение редактора секции отличается от значения В ФАЙЛЕ (без submit()/валидации).

        Эталон — файл, а не резолвнутый конфиг: иначе поле со ссылкой ${ENV:VAR} вечно
        «изменено» (резолв ≠ ссылка) и каждое сохранение выписывает в файл литерал."""
        if section == _CONNECTION:
            try:
                return self._connection_editor().is_dirty()
            except NoMatches:
                return False
        if section != _MODELS:
            # Ветка по умолчанию была безусловной и спрашивала ModelEditor у любой
            # секции — после поглощения легаси-«Модели» это единственный владелец
            # вопроса, и спрашивать его от чужого имени нельзя.
            return False
        try:
            picked = self._legacy_model_editor().peek()
        except NoMatches:
            return False
        if picked is None:
            return False
        return picked != self._raw_value("model")

    def _acting_choice(self) -> str:
        """Выбор модели, которым агент работает ПРЯМО СЕЙЧАС: сессионный, если он
        задан, иначе записанный в конфиге. Тот же порядок, что у
        `TuiApp._choice_provider` — второго прочтения одного факта здесь не заводим.

        Экран открывается с БАЗОЙ (`base_settings()`, без сессионных переопределений),
        поэтому спрашивать базу экрана нельзя ни одной витрине, обещающей «сейчас»:
        `/level` в файл не пишет, и записанное там — не то, что работает."""
        from gc_core.config_modules.model_registry import configured_choice

        session = (getattr(self.app, "_session_overrides", None) or {}).get(
            "model"
        )
        return str(session or configured_choice(self._cfg_settings()))

    # --- probe-инфраструктура (спека B2.1: слоты + generation) ---
    def _probe_args_from_config(self, config=None):
        """(адрес, токен, ssl, модель, прокси) действующего конфига. По умолчанию —
        runtime экрана; `config` передаёт тот, кто спрашивает про ДРУГОЙ стек (под-вид
        открытого профиля — про свой, см. `_open_profile_config`): его настройки
        либо подключение, собранное по документам (`dict`,
        `_open_profile_connection_by_documents`)."""
        from gc_core.onboarding import (
            MODEL_SENTINEL,
            TOKEN_SENTINEL,
            configured_endpoint,
        )

        if isinstance(config, dict):
            url = config.get("completions_url") or ""
            headers = config.get("chat_completion_headers") or {}
            proxy = config.get("proxy") or None
            model = config.get("model") or ""
            use_ssl = config.get("use_ssl", True)
        else:
            settings = settings_of(
                self._settings if config is None else config
            )
            backend = settings.backend
            url = backend.completions_url or ""
            headers = backend.chat_completion_headers or {}
            proxy = backend.proxy or None
            model = settings.agent.model or ""
            use_ssl = backend.use_ssl
        endpoint = configured_endpoint(url)
        auth = headers.get("Authorization")
        if auth in (TOKEN_SENTINEL, ""):
            auth = None
        if (
            model == MODEL_SENTINEL
        ):  # плейсхолдер шаблона — не слать его как имя модели
            model = ""
        return endpoint, auth, use_ssl, model, proxy

    def _presets_of(self, config=None) -> list:
        """Пресеты адреса подключения того же стека, что `_probe_args_from_config`:
        настроек (своих или открытого профиля) либо подключения по документам —
        там пресеты уже проверены по форме (`_open_profile_connection_by_documents`),
        нет их — кодовый дефолт."""
        from gc_core.config_modules.endpoint_presets import (
            EndpointPreset,
            default_endpoint_presets,
            endpoint_presets_from_config,
        )

        if isinstance(config, dict):
            presets = config.get("endpoint_presets")
            if not presets:
                return default_endpoint_presets()
            return [EndpointPreset.model_validate(item) for item in presets]
        return endpoint_presets_from_config(
            self._settings if config is None else config
        )

    def _start_config_probe(self) -> None:
        endpoint, auth, verify, model, proxy = self._probe_args_from_config()
        if not endpoint:
            self._probe_config = None
            return
        self._config_gen += 1
        gen = self._config_gen
        self._probe_in_flight = True
        self._refresh_menu_rows()

        def _work() -> None:
            from gc_tui.services.settings_probe import (
                ProbeResult,
                probe_connection,
            )

            try:
                result = self._probe_fn(
                    endpoint,
                    auth,
                    verify,
                    model,
                    config_proxy=proxy,
                )
            except Exception as exc:
                result = ProbeResult(
                    "unknown",
                    None,
                    None,
                    0,
                    f"внутренняя ошибка проверки: {exc}",
                )
            auth_result = None
            if result.models is not None:
                # Ответил справочник моделей — а он бывает публичным (OpenRouter отдаёт
                # `/models` без токена), и об авторизации его «200» не говорит ничего:
                # ни что токен приняли, ни что он вообще нужен. Только здесь вердикта
                # и нет — 401 справочника уже приговор, а «ок» без моделей проба
                # получила фолбэком по самому chat/completions, пройдя авторизацию.
                # Догоняем ровно эту неизвестность — той же differential-пробой, что
                # судит секция, иначе хаб и секция разойдутся вердиктом.
                try:
                    auth_result = probe_connection(
                        endpoint,
                        auth,
                        verify,
                        model,
                        config_proxy=proxy,
                        probe_fn=self._auth_probe_fn,
                    )
                except Exception:
                    auth_result = (
                        None  # не смогли — хаб про токен просто промолчит
                    )
            if auth_result is not None and auth_result.auth_state is not None:
                # Вливаем вердикт в ТОТ ЖЕ результат, а не держим вторым полем: два
                # поля про одну пробу расходятся при первом же обновлении одного из
                # них. Список моделей остаётся — по нему живёт глиф «Модель».
                result = replace(result, auth_state=auth_result.auth_state)
            self.app.call_from_thread(self._on_config_probe, result, gen=gen)

        self.run_worker(_work, thread=True, name="settings-config-probe")

    def _on_config_probe(self, result, *, gen: int) -> None:
        if gen != self._config_gen or not self.is_mounted:
            return  # устаревший/посмертный — отбросить
        self._probe_in_flight = False
        self._stop_wave()
        self._probe_config = result
        self._refresh_menu_rows()

    def _refresh_menu_rows(self) -> None:
        if self._view != "menu":
            return
        self._menu_pass_snapshot = (
            None  # новый проход — цель перечитывается один раз
        )
        for i, row in enumerate(self.query(_SEL_SET_MENUROW)):
            if i >= len(SECTIONS):
                # Транзиентный двойной комплект строк: отложенный call_after_refresh
                # (carry-fix выше) может сработать ДО того, как Prune старых 5 строк
                # применится — query видит старые+новые вперемешку (до 10), а
                # SECTIONS всего len(SECTIONS) — без guard это IndexError.
                continue
            try:
                row.query_one(".set-rowtext", Static).update(
                    self._menu_rowtext(i, i == self._menu_idx)
                )
                row.query_one(".set-glyph", Static).update(
                    self._menu_glyph_text(i)
                )
            except NoMatches:
                continue  # дети строки ещё не смонтированы (первый тик on_mount)
            (row.add_class if i == self._menu_idx else row.remove_class)(
                "selected"
            )
        if self._probe_in_flight:
            self._start_wave()  # пока идёт probe — крутим волну (стоп в _wave_tick)

    # --- draft-probe секции (Ctrl+Space / умный Enter, спека v2 §3.1) ---
    def _start_draft_probe(
        self, section: int, on_done=None, *, manual: bool = False
    ) -> None:
        """Differential-probe «Подключения»: контрольный запрос БЕЗ токена определяет,
        требует ли сервер авторизацию; только при 401 проверяем сам токен. Так
        честно отличаем «сервер не требует токен» от «токен принят/отклонён».

        Endpoint и token берутся из ПОЛЯ (ConnectionEditor.peek(), draft), а не из
        сохранённого конфига — иначе проверялась бы несуществующая комбинация
        «новый адрес + старый токен». Сырой токен из поля уходит в probe_connection
        без Bearer-нормализации: проба сама перебирает формы.

        `manual` — проверку запустил человек (`Ctrl+Space`, `Enter`): её строки —
        ответ на клавишу и подводятся в кадр. Автопроверка кадр не двигает (см.
        `_set_probe_line`)."""
        show = self._show_probe_line if manual else self._set_probe_line
        endpoint_cfg, auth_cfg, verify, model, proxy_cfg = (
            self._probe_args_from_config()
        )
        url, auth_idx, token_raw, proxy_draft = (
            self._connection_editor().peek()
        )
        endpoint = url or endpoint_cfg  # поле пусто → действующий (для ссылок)
        if not endpoint:
            show(
                Text(
                    "сначала настройте адрес — проверка стучится на него",
                    style=DEEP_DIM,
                )
            )
            return
        if auth_idx == 1:
            token = None  # без авторизации
        elif token_raw:
            token = token_raw  # сырой — probe_connection перебирает формы
        elif self._connection_editor().token_cleared:
            token = None  # Ctrl+U: сохранённый стёрт ещё до записи
        else:
            token = auth_cfg  # поле пусто → действующий токен
        # draft proxy: поле → иначе config; пустое поле = «не задано» → env через резолвер
        proxy = proxy_draft if proxy_draft else proxy_cfg
        # Снимок ПОЛЕЙ (не резолва) — по нему автопроба ухода с поля отличает
        # «значения изменились» от ходьбы по форме; пишем здесь, а не на местах
        # вызова, чтобы автопроба входа в секцию и ручные пробы тоже считались.
        # Собирает его сам редактор: в снимок входит и то, чего нет в полях
        # (Ctrl+U, галка «без авторизации»), — иначе он врёт про вход пробы.
        self._last_probed_snapshot = self._connection_editor().probe_snapshot()
        self._draft_gen += 1
        gen = self._draft_gen
        show(Text("проверяю…", style=DEEP_DIM))

        def _work() -> None:
            from gc_tui.services.settings_probe import (
                ProbeResult,
                probe_connection,
            )

            try:
                result = probe_connection(
                    endpoint,
                    token,
                    verify,
                    model,
                    config_proxy=proxy,
                    probe_fn=self._auth_probe_fn,
                )
            except Exception as exc:
                result = ProbeResult(
                    "unknown",
                    None,
                    None,
                    0,
                    f"внутренняя ошибка проверки: {exc}",
                )
            self.app.call_from_thread(
                self._on_draft_probe,
                result,
                gen=gen,
                section=_CONNECTION,
                on_done=on_done,
                manual=manual,
            )

        self.run_worker(_work, thread=True, name="settings-draft-probe")

    def _on_draft_probe(
        self, result, *, gen, section, on_done, manual: bool = False
    ) -> None:
        if (
            gen != self._draft_gen
            or not self.is_mounted
            or self._view != section
        ):
            return
        if result.auth_state == "accepted":
            ed = self._connection_editor()
            if ed.apply_accepted_auth(result.accepted_auth):
                # Принятая форма меняет черновик программно. Старое
                # подтверждение ENV/FILE-сохранения держит прежний снимок и
                # после такой правки могло записать уже не показанное значение.
                self._cancel_env_confirm()
                self._last_probed_snapshot = ed.probe_snapshot()
                self._update_dirty()
        self._draft_checked = result
        show = self._show_probe_line if manual else self._set_probe_line
        show(connection_line_text(result))
        if on_done is not None:
            on_done(result)

    def _set_probe_line(self, text: Text) -> Static | None:
        """Строка вердикта проверки — БЕЗ подводки кадра.

        Кадр двигает только ответ на клавишу (`_show_probe_line`). Автопроверку
        (вход в секцию, уход с поля) и кадры анимации «волны» человек не
        запускал — он смотрит на поле под курсором. Строка, подводившая себя
        сама на КАЖДОМ обновлении, уводила кадр из-под курсора, а при входе в
        секцию ещё и гонялась с подводкой курсора: кто успевал последним, тот и
        оставался в кадре (снапшот `connection_low` падал первым прогоном и
        проходил на перезапуске).
        """
        try:
            line = self.query_one(".set-probe", Static)
            line.update(text)
        except NoMatches:
            try:
                frame = self.query_one(_SEL_SET_FRAME, Vertical)
            except NoMatches:
                return None
            line = Static(text, classes=_CLS_SET_HINT_SET_DYN_SET_PROBE)
            frame.mount(line)
        return line

    def _show_probe_line(self, text: Text) -> None:
        """Строка вердикта как ОТВЕТ НА КЛАВИШУ проверки — подвести в кадр.

        Строка стоит последней в карточке: на низком окне ответ на нажатие
        оказывался под сгибом, и выглядело это как «проверка ничего не
        сделала». Подводка — разовая, общим `reveal` (сообщение, а не вопрос:
        кадр она не держит, см. `form_frame`)."""
        line = self._set_probe_line(text)
        if line is not None:
            reveal(line)

    def _reset_draft_state(self) -> None:
        self._draft_checked = None
        self._cancel_env_confirm()  # правка обесценивает и подтверждение перезаписи ссылки
        self._draft_gen += 1  # правка значения обесценивает летящий draft-probe (config-probe не трогаем)
        # Снимок живёт ровно столько, сколько проба, которая его записала: её
        # результат сейчас обесценен, значит значения снова НЕ проверены. Оставить
        # снимок значило бы запретить гейтом дребезга повтор пробы, которой уже не
        # будет, — и строка вердикта осталась бы на «проверяю…» навсегда.
        self._last_probed_snapshot = None
        if self._view in _PROBE_SECTIONS:
            try:
                self.query_one(_SEL_SET_FOOTER, Static).update(
                    self._footer_text()
                )
            except NoMatches:
                pass

    # --- guard записи ---
    def _writable(self) -> bool:
        return self._target_writable(self._config_path)

    def _writable_or_mcp(self) -> bool:
        """Секция MCP пишет в config.local.yml (не в user config) — не блокируется
        write-guard пакетного шаблона, применимым к секциям 0/1."""
        return self._view == _MCP or self._writable()

    def _target_writable(self, target) -> bool:
        """Тот же guard пакетного шаблона, что `_writable`, но по ЦЕЛИ записи.

        Секция «Профили» пишет не всегда в `self._config_path`: цель бывает
        проектной (`.gc/config.yml`, всегда писуча и создаётся при отсутствии) —
        поэтому проверять надо конкретный файл, а не путь запуска. Без этого
        запуск на пакетном шаблоне (GC_HOME не настроен, `--init-config` не
        запускали) писал бы `profile:`/`level:` внутрь установленного пакета.

        Пустой путь — не цель записи (паритет с прежним `_writable`, чей гард здесь
        был потерян: `Path(None)` роняет TypeError, `Path("")` резолвится в cwd и
        объявлял бы запись разрешённой)."""
        from pathlib import Path

        from gc_core.config import _default_config_path

        if not target:
            return False
        try:
            return (
                Path(target).resolve()
                != Path(_default_config_path()).resolve()
            )
        except OSError:
            return False

    _TEMPLATE_WRITE_REFUSAL = (
        "✗ не сохранено: конфиг недоступен для записи — используется пакетный "
        "шаблон (настройте GC_HOME или запустите gc-ua --init-config)"
    )
    # Тот же отказ для файловых операций секции «Профили», но подставляются И
    # глагол («не создано»/«не переименовано»/«не удалено»), И объект: недоступен
    # бывает либо каталог профилей, либо файл конфига со ссылкой на профиль —
    # называть второе первым конкретно неверно (`_profile_write_block`).
    _PROFILE_FILE_WRITE_REFUSAL = "✗ не {done}: {reason} — используется пакетный шаблон (настройте GC_HOME или запустите gc-ua --init-config)"

    _SECRET_FIELD = "Authorization"  # действующее значение секрета маскируем, не печатаем как есть

    def _effective_display(self, key: str, effective) -> str:
        """Действующее значение поля для подписи-перекрытия: секрет — маской
        (onboarding.display_secret — литерал прячет, ссылку ${ENV:VAR} показывает как есть).

        Значение приходит из ЧУЖОГО файла (оверлея) — его тип ничем не гарантирован:
        `Authorization: 12345` — валидный YAML. Приводим к строке, иначе маскирование
        (header.split) роняет монтирование секции."""
        if key != self._SECRET_FIELD:
            return effective
        if not effective:
            return "не задано"
        from gc_core.onboarding import display_secret

        return display_secret(
            effective if isinstance(effective, str) else str(effective)
        )

    def _field_hint_specs(self, key: str) -> list[tuple[str, str]]:
        """(текст, css-класс) подписей под полем — только когда есть что сказать.

        Ссылка ${ENV:VAR} → её статус. Поле перекрыто оверлеем → действующее значение
        и источник. Случаи складываются: в файле ссылка И поле перекрыто → обе строки.
        Обычное значение из базового файла → подписей нет, экран чист.

        Данными, а не монтированием: те же подписи рисует у себя секция «Модели» —
        легаси-редактор уехал внутрь её тела, а знание о происхождении полей живёт
        здесь (`_origins`)."""
        from gc_core.onboarding import display_value, env_ref_status

        specs: list[tuple[str, str]] = []
        raw = self._raw_value(key)
        if env_ref_status(raw):
            specs.append((display_value(raw), _CLS_SET_HINT_SET_DYN))
        origin = self._origin(key)
        if origin is not None and origin.is_overridden:
            from gc_core.config_origin import overlay_label

            source = overlay_label(origin)
            shown = self._effective_display(key, origin.effective)
            specs.append(
                (
                    f"⚠ действует {shown} — перекрыто {source}",
                    "set-hint set-dyn set-warnflash",
                )
            )
        return specs

    def _mount_field_hints(self, frame: Vertical, key: str) -> None:
        for text, cls in self._field_hint_specs(key):
            frame.mount(Static(text, classes=cls))

    def _mount_section_body(self, frame: Vertical, section: int) -> None:
        from gc_core.onboarding import TOKEN_SENTINEL

        if section in _WRITE_GATED_SECTIONS and not self._writable():
            frame.mount(
                Static(
                    "конфиг недоступен для записи: используется пакетный шаблон "
                    "(настройте GC_HOME или запустите gc-ua --init-config)",
                    classes=_CLS_SET_HINT_SET_DYN,
                )
            )
            return
        if section == _CONNECTION:
            from gc_tui.editors.connection import ConnectionEditor

            auth = self._raw.get("Authorization")
            header = (
                None if auth == TOKEN_SENTINEL else auth
            )  # None=сентинел; ""=без авт.; иначе значение
            from gc_core.config_modules.endpoint_presets import (
                endpoint_presets_from_config,
            )

            frame.mount(
                ConnectionEditor(
                    url_current=self._raw_value("completions_url"),
                    auth_header=header,
                    proxy_current=self._raw_value("proxy"),
                    presets=endpoint_presets_from_config(self._settings),
                )
            )
            self._mount_field_hints(frame, "completions_url")
            self._mount_field_hints(frame, "Authorization")
            self._mount_field_hints(frame, "proxy")
            self._mount_shadow_hint(frame)
            frame.mount(
                Static(
                    connection_line_text(self._probe_config),
                    classes=_CLS_SET_HINT_SET_DYN_SET_PROBE,
                )
            )
        elif section == _MODELS:
            from gc_core.config import _default_config_path
            from gc_core.config_modules.endpoint_presets import (
                endpoint_presets_from_config,
            )
            from gc_core.utils.user_text import read_user_text_or_none

            from gc_tui.editors.models_section import ModelsSectionEditor

            endpoint, auth, verify, _m, proxy = self._probe_args_from_config()
            # шаблона нет или он не читается — якорь секции синтезирует сам writer
            template = read_user_text_or_none(_default_config_path()) or ""
            frame.mount(
                ModelsSectionEditor(
                    target=self._config_path,
                    template=template,
                    # Что действует НАД основным конфигом: реестр слоя выше и сам слой.
                    # Секции это нужно ровно затем, чтобы не обещать выключенные уровни
                    # там, где они работают чужими записями, — а знание о слоях есть
                    # только у происхождения (`_registry_origin`), не у снимка конфига.
                    overlay_origin=self._registry_origin,
                    effective_url=endpoint,
                    effective_auth=auth,
                    verify=verify,
                    config_proxy=proxy,
                    presets=endpoint_presets_from_config(self._settings),
                    global_config=self._settings,
                    active_choice=self._acting_choice(),
                    legacy_editor_factory=self._make_legacy_model_editor,
                    legacy_hints=self._field_hint_specs("model"),
                    on_change=self._models_changed,
                    on_notice=self._models_notice,
                    on_legacy_save=self._save_legacy_model,
                )
            )
        elif section == _MCP:
            from gc_tui.editors.mcp_section import McpSectionEditor
            from gc_tui.services.mcp_manager import McpManager

            mgr = McpManager(settings=self._settings)
            frame.mount(
                McpSectionEditor(manager=mgr, on_change=self._mcp_changed)
            )
        elif section == _VOICE:
            self._mount_voice_section(frame)
        elif section == _MODES:
            self._mount_modes_section(frame)
        elif section == _LOOK:
            self._mount_look_section(frame)
        elif section == _UPDATE:
            self._mount_update_section(frame)
        else:
            self._mount_profiles_section(frame)

    # --- секция «Модели»: швы экрана (монтаж, запись легаси-поля, витрины) ------
    def _models_editor(self):
        """Свежесмонтированный `ModelsSectionEditor` либо None (read-only конфиг /
        первый кадр). ПОСЛЕДНИЙ матч — по той же причине, что у `_connection_editor`:
        снесённый предыдущим рендером ещё в дереве, а решения принимает живой.

        Метод — про КЛАСС, а не про секцию: тот же редактор стоит телом секции
        «Модели» и под-видом открытого профиля (формы одни, целей две), а на экране
        в каждый момент смонтирован ровно один."""
        from gc_tui.editors.models_section import ModelsSectionEditor

        editors = list(self.query(ModelsSectionEditor))
        return editors[-1] if editors else None

    def _route_registry_editor(self, ed, event: events.Key) -> bool:
        """Клавиши редактора реестра — секции «Модели» И под-вида открытого профиля
        (редактор один, значит и роутинг один: разойдясь, они дали бы разные клавиши
        одним и тем же формам). True — обработчик экрана должен завершиться.

        Образец — блок MCP: секция ведёт СВОИ клавиши целиком, у неё свои
        под-состояния (форма записи, под-форма резервной, пикер, confirm), а экран
        только роутит. Читается сверху вниз тем же порядком: confirm глотает всё →
        навигация → действия."""
        # Подтверждение перезаписи ${ENV:VAR} держит СНИМОК значений на момент
        # Enter. Свои две клавиши — Enter (подтвердить) и Esc (отменить) — до
        # сюда не доходят: их перехватывают гейты выше. Значит любая клавиша,
        # добравшаяся до блока, из этого состояния уводит: двигает курсор,
        # перечитывает список (выбор падает на якорь, если модель пропала из
        # ответа), открывает форму записи. Снимок после неё — уже не про то,
        # что показано, и следующий Enter записал бы ПРЕЖНЮЮ модель поверх
        # ссылки на переменную окружения.
        #
        # Отмена стоит ОДИН раз здесь, а не в каждой ветке: так закрывается
        # класс дефекта, а не три его экземпляра — следующая клавиша секции
        # получит правило даром. Спешить с отменой безопасно: она fail-safe
        # (ссылка остаётся в файле), а следующий Enter поднимет баннер заново.
        # (В профиле снимку взяться неоткуда — плоских полей у него нет; вызов
        # там безобиден и правило остаётся одним на обе цели.)
        if ed.awaiting_confirm:
            event.stop()
            if event.key == "enter":
                leaving = ed._dirty_exit_pending
                ed.enter()  # подтверждение удаления или перезаписи ссылки
                if leaving and self._pending_section_delta is not None:
                    delta = self._pending_section_delta
                    self._pending_section_delta = None
                    self._switch_section(delta)
                self._refresh_footer()
            return True
        self._cancel_env_confirm()
        if event.key == "f1" and ed.open_form_help():
            event.stop()
            event.prevent_default()
            self._refresh_footer()
            return True
        if event.key == "f2" and ed.open_form_card():
            event.stop()
            event.prevent_default()
            self._refresh_footer()
            return True
        if event.key == _SAVE_KEY and ed.save_form():
            event.stop()
            event.prevent_default()
            self._refresh_footer()
            return True
        if event.key in ("up", "down", "enter") and ed.form_keeps_key(
            event.key
        ):
            # YAML-поле main/auxiliary: Enter — перевод строки, ↑/↓ ходят по
            # тексту, пока курсор не у края. Клавишу виджету, не форме.
            return False
        if event.key in ("up", "down"):
            event.stop()
            ed.move(-1 if event.key == "up" else 1)
            self._refresh_footer()
            return True
        if event.key in (_KEY_ALT_DOWN, _KEY_CTRL_DOWN):
            # Карта проб §2.5: подбор списка (форма — свой, легаси-редактор —
            # свой). Съедаем, ТОЛЬКО если редактору было что раскрыть, — он сам
            # об этом и отвечает (`alt_down` возвращает признак действия). Иначе
            # клавиша проваливается к прокрутке тела: футер обещает «Ctrl+↑/↓ —
            # прокрутка», и молча съеденная клавиша это обещание нарушала (тот же
            # класс, что у адреса подключения и голоса).
            #
            # В поле модели прокрутки клавиша не получит и так: там `alt_down`
            # всегда перечитывает список. Строки под рамкой поля довозит подводка
            # кадра (`REVEAL_WITH_TAIL` у `ModelEditor`).
            if ed.alt_down():
                event.stop()
                self._refresh_footer()
                return True
            return False
        if event.key in (_KEY_CTRL_AT, _KEY_CTRL_SPACE, _KEY_CTRL_AT_SIGN):
            # Проба есть только у формы записи; на списке и в легаси-редакторе
            # её нет (§5.2) — клавиша съедается молча, ничего не обещая.
            event.stop()
            ed.probe()
            return True
        if event.key == _KEY_CTRL_U:
            # Вернуть записи общее подключение — адрес, токен и галку разом (§5).
            # Перехватывать приходится здесь по той же причине, что и в
            # «Подключении»: `Input` держит на клавише `delete_left_all`, а поле
            # токена пустое — секрет живёт маской в placeholder, и штатное поведение
            # там вхолостую. Условие «наш ли это случай» решает форма (пара своя и
            # курсор на одном из её полей); не наш — клавишу НЕ съедаем, и в полях
            # легаси-редактора её очистка работает как обычно.
            if not ed.clear_token():
                return False
            event.prevent_default()
            event.stop()
            # Пара ушла в общую, и вместе с ней ушло обещание клавиши: футер формы
            # позиционный и гейтится тем же предикатом, что она сама.
            self._refresh_footer()
            return True
        if event.key == "enter":
            event.stop()
            ed.enter()
            self._refresh_footer()
            return True
        if event.key == "delete" and not isinstance(
            self.focused, (Input, TextArea)
        ):
            event.stop()
            ed.request_remove()
            self._refresh_footer()
            return True
        if event.key == "space":
            if ed.space():
                event.stop()  # галка формы; иначе пробел печатается
            return True
        if event.key in ("alt+n", "alt+т", "alt+N", "alt+Т") or (
            event.key in ("n", "т", "N", "Т")
            and not isinstance(self.focused, (Input, TextArea))
        ):
            event.stop()
            ed.new_entry()
            self._refresh_footer()
            return True
        return False

    def _legacy_model_editor(self):
        """Свежесмонтированный легаси-редактор модели.

        ПОСЛЕДНИЙ матч, а не `query_one`: секция пересоздаёт его на КАЖДУЮ сборку
        тела, а `remove()` в Textual отложенный — сразу после пересборки в дереве
        два редактора, и `query_one` молча отдаёт первый, обречённый. Здесь это
        стоило бы ровно того же, что и у `_connection_editor`: в конфиг уехало бы
        значение прошлого рендера (находка 2026-08-07)."""
        from gc_tui.editors.model import ModelEditor

        editors = list(self.query(ModelEditor))
        if not editors:
            raise NoMatches("ModelEditor не смонтирован")
        return editors[-1]

    def _make_legacy_model_editor(self):
        """Легаси-редактор модели (`agent.model`) — тот же, что стоял в секции
        «Модель» до поглощения: в пустом реестре он и есть секция, при живом реестре
        — под-форма строки «резервная модель»."""
        from gc_core.model_picker import fetch_models

        from gc_tui.editors.model import ModelEditor
        from gc_tui.services.model_list_cache import make_cached_fetch

        # За списком моделей ходим ДЕЙСТВУЮЩИМИ (резолвнутыми) адресом и токеном;
        # current/active — сырые: именно они пишутся в файл.
        endpoint, auth, verify, _m, proxy = self._probe_args_from_config()
        model = self._raw_value("model")
        origin = self._origin("model")
        return ModelEditor(
            current=model,
            endpoint=endpoint,
            auth=auth,
            verify=verify,
            fetch=make_cached_fetch(fetch_models),
            active=model,
            config_proxy=proxy,
            # Перекрытую модель в списке нельзя звать «текущей»: действует оверлей,
            # а подпись ниже говорит именно это — экран не должен спорить сам с собой.
            active_overridden=bool(
                origin is not None and origin.is_overridden
            ),
        )

    def _save_legacy_model(self) -> None:
        """Enter в легаси-редакторе секции «Модели». Пишем `agent.model` тем же
        `_do_save`, что и раньше: плоское поле, а не реестр, — значит и подтверждение
        перезаписи ${ENV:VAR}, и предупреждение о перекрытии те же."""
        if not self._writable():
            self._notice("конфиг недоступен для записи")
            return
        values = self._collect_values(_MODELS)
        if values is None:
            return
        if not values:
            # Ctrl+S всегда завершает форму. Если значение не менялось, файл не
            # трогаем, но возвращаемся туда же, откуда форма была открыта.
            editor = self._models_editor()
            if editor is not None and not editor.registry_empty:
                editor._legacy_open = False
                editor._rebuild()
            else:
                self._close_or_back()
            return
        # Пустое состояние — это САМА секция, и запись из неё завершает работу с ней
        # (возврат в хаб, как у прежней «Модели»). Под-форма резервной открыта из
        # списка записей — ей возвращаться туда же, откуда пришли.
        editor = self._models_editor()
        self._do_save(
            _MODELS,
            values,
            back_to_menu=editor is None or editor.registry_empty,
        )

    def _models_changed(self) -> None:
        """Секция записала в файл: сырые значения и происхождение перечитать, конфиг
        приложения пересобрать — витрины (футер, `/config`, превью хаба) обязаны
        показывать записанное, не прежнее."""
        self._raw = self._read_raw()
        self._menu_pass_snapshot = (
            None  # файл изменился — снимок прохода протух
        )
        self._profiles_disk_cache = {}  # ... и дисковые сводки секции «Профили» тоже
        if self._profiles_open is not None:
            # Запись меняет и СОСТАВ превью оверлеев: реестр стал непуст (или опустел)
            # — формы `slots`/`assign` поднялись (или ушли), а превью говорит ровно
            # про то, чего формы не показывают.
            self._refresh_profile_overlay()
        try:
            self._on_apply()
            self._pull_base_from_app()
        except Exception as exc:
            self._notice(f"записано, но конфиг не перечитан: {exc}")
            return
        self._origins = self._read_origins()

    def _models_notice(self, text: str, *, ok) -> None:
        """Итог операции секции — тем же каналом, что и у `_do_save`: удача заметной
        плашкой (✓ ставит сам `_flash`), отказ и предупреждение — строкой-notice,
        глиф в них уже свой."""
        if ok:
            self._flash(text)
        else:
            self._notice(text)

    def _shadow_hint_text(self) -> str | None:
        """«действует профиль work / реестр моделей — эти поля база для незаданного»
        в секции «Подключение» — плоские ключи легаси остаются источником
        наследования, но не действующим резолвом, когда есть merged-реестр (с
        профилем или без него) или активный профиль. В «Моделях» этой подсказки нет
        (решение 6 волны B): при живом реестре `agent.model` базой наследования не
        является, и строка резервной модели говорит про это своими словами.

        Профиль важнее голого реестра (реестр без профиля — частный случай, но явный
        выбранный профиль — более конкретная причина «эти поля не действуют»).
        Источник — effective_settings(): сессионный выбор профиля обязан отразиться в
        подсказке немедленно, как и everywhere в TUI."""
        loaded = (
            self.app.effective_settings()
            if hasattr(self.app, "effective_settings")
            else self._settings
        )
        profile_name = runtime_of(loaded).active_profile or None
        if profile_name:
            return f"действует профиль {profile_name} · эти поля — база для незаданного"
        if settings_of(loaded).models:
            return "действует реестр моделей · эти поля — база для незаданного"
        return None

    def _mount_shadow_hint(self, frame: Vertical) -> None:
        text = self._shadow_hint_text()
        if text is not None:
            frame.mount(Static(text, classes=_CLS_SET_HINT_SET_DYN))

    # --- секция «Голосовой ввод»: статус + две строки режима + поля своего endpoint ---
    _VOICE_REMOVE_CONFIRM = (
        "Удалит окружение сервиса (~0.4 ГБ) и отключит конфиг; модели в общем "
        "кэше останутся. Enter — удалить · Esc — отмена"
    )
    # Установка так же необратима с виду, как удаление: качает ~0.9 ГБ и уводит экран в
    # терминал на несколько минут. Спрашиваем тем же способом — канон confirm секции.
    _VOICE_INSTALL_CONFIRM = (
        "Поставит окружение сервиса и скачает модель (~0.9 ГБ); ход установки покажет "
        "в терминале. Enter — установить · Esc — отмена"
    )

    _VOICE_SNAPSHOT_TTL = 1.0  # с: снимок живёт кадр-другой, не дольше

    def _voice_snapshot(self, force: bool = False):
        """Статус батарейки БЕЗ похода в сеть: дешёвый снимок + последняя известная
        живость порта.

        Живость меряет воркер (_kick_voice_alive) — connect к мёртвому порту на
        машинах, где файрвол молча роняет пакеты, сгорает по полному таймауту
        (~0.5 с, замерено живьём), и пока проверка жила внутри снапшота, каждый ход
        курсора в /settings замерзал, стоило сервису лечь. Кэш остаётся коротким
        (state.json перечитывается после действий), `force` его сбрасывает; живость
        force НЕ меряет — только воркер."""
        now = time.monotonic()
        if (
            not force
            and self._voice_snapshot_value is not None
            and now - self._voice_snapshot_at < self._VOICE_SNAPSHOT_TTL
        ):
            return self._voice_snapshot_value
        from dataclasses import replace

        s = self._voice_setup.status_cheap(self._settings)
        if s.installed and s.service_alive is None:
            # Дешёвый снимок живости не знает (None) — доносим последнюю замеренную.
            # Фасад, который знает сразу (тестовый), экран не поправляет.
            s = replace(s, service_alive=self._voice_alive)
        self._voice_snapshot_value = s
        self._voice_snapshot_at = now
        return self._voice_snapshot_value

    def _voice_alive_tick(self) -> None:
        """Интервал живости: меряем только пока человек смотрит на голосовую секцию —
        в остальное время статус освежает вход в секцию и действия."""
        if self._view == _VOICE:
            self._kick_voice_alive()

    def _kick_voice_alive(self) -> None:
        """Замерить живость порта ФОНОМ и донести результат на UI-поток.

        Единственный владелец сокета в этой секции (канон проб экрана: run_worker +
        call_from_thread + поколение против гонок). Повторный вызов при летящей пробе
        безопасен: свежее поколение обесценит старый результат."""
        self._voice_alive_gen += 1
        gen = self._voice_alive_gen

        def _work() -> None:
            try:
                alive = self._voice_setup.probe_alive()
            except Exception:
                alive = None
            self.app.call_from_thread(self._on_voice_alive, alive, gen)

        self.run_worker(_work, thread=True, name="voice-alive-probe")

    def _on_voice_alive(self, alive, gen: int) -> None:
        """Живость приехала: обновить снимок и ТОЧЕЧНО перерисовать, где видно."""
        if gen != self._voice_alive_gen or not self.is_mounted:
            return
        changed = alive is not self._voice_alive
        self._voice_alive = alive
        if not changed:
            return
        self._invalidate_voice_snapshot()
        if self._view == _VOICE and not self._voice_awaiting_confirm():
            self._refresh_voice_section(kick_alive=False)
        elif self._view == "menu":
            # Точечно, как refresh_update_glyph: полная пересборка (_render_view)
            # видна глазом — мигание меню через ~полсекунды после каждого захода.
            self._refresh_menu_rows()  # глиф ✓ у строки меню судит по живости

    def _invalidate_voice_snapshot(self) -> None:
        """Состояние изменилось не нашими руками (перечитан конфиг, поднялся сервис) —
        кэш обесценен: следующий читатель обязан спросить заново."""
        self._voice_snapshot_value = None

    def _voice_raw_cloud(self) -> dict:
        """voice-секция СЫРОГО config.yml: self._raw/_read_raw ограничены
        FIELD_ANCHOR_PATHS визарда (completions_url/Authorization/model) и voice.*
        не видят — читаем документ напрямую (config_raw.read_document уже даёт
        {} на нечитаемый/битый файл)."""
        if not self._config_path:
            return {}
        from gc_core import config_raw

        return config_raw.read_document(self._config_path).get("voice") or {}

    def _mount_voice_section(self, frame: Vertical) -> None:
        """Хаб голосового ввода: две строки режима (радио-язык мастера) со статусом в
        приглушённом хвосте + поля ТОЙ строки, на которой стоит курсор.

        Отдельной шапки статуса нет: она повторяла бы название режима и модель, которую
        видно в поле «Модель». Действий-строк («Установить…», «Режим: …») тоже нет —
        режим выбирается там же, где показан, а установка/включение это Enter по строке."""
        s = self._voice_snapshot()
        self._voice_status = (
            s  # гейты клавиш смотрят на ТО ЖЕ, что видит человек
        )
        if not self._voice_cursor_placed:
            # Первый вход — на строке ДЕЙСТВУЮЩЕГО режима: иначе облачный юзер без
            # батарейки первым кадром видел бы чужую панель («Enter — установить»),
            # а не свои настройки. Дальше курсор помнит место сам (повторный вход).
            self._voice_cursor_placed = True
            self._voice_row = _VOICE_MODE_ROW.get(s.mode, _VOICE_ROW_LOCAL)
            self._voice_pane = (
                "cloud" if self._voice_row == _VOICE_ROW_CLOUD else "local"
            )
        if _VOICE_GROUP_FIELDS not in self._voice_groups():
            # Статус мог измениться, пока секции не было на экране (сервис снесли из
            # терминала): группа полей под запомненным курсором исчезла — возвращаем его
            # в строки режима. Хранимую группу правим ЯВНО: `_voice_cursor_group`
            # только сверяет её с наличными, и вернувшиеся поля забрали бы курсор себе.
            self._voice_group = _VOICE_GROUP_VARIANTS
        frame.mount(
            Static(
                Text("Режим:", style=DEEP_DIM),
                classes="set-info set-dyn set-spaced-gap",
            )
        )
        # Перекрытие — свойство ВСЕЙ секции, а не строки под курсором: профиль и
        # project-слой ложатся выше managed, где живёт адрес локальной батарейки, и
        # перебивают ОБА режима. Стоя под вариантами, строка прилипала к соседнему и
        # читалась как его свойство (живой смок 2026-08-26): курсор на «Локальном
        # сервисе» — предупреждение про адрес, который человек считает чужим.
        self._mount_voice_override_hint(frame)
        highlight = self._voice_highlight_variants()
        for i in range(len(_VOICE_ROWS)):
            # Подсветка — строго по курсору (ушёл в поля — её нет ни у одной строки);
            # ▸ живёт по владельцу панели, см. `_voice_row_text`.
            cls = "set-row set-dyn set-spaced set-voicerow" + (
                _CLS_SELECTED if i == self._voice_row and highlight else ""
            )
            frame.mount(Static(self._voice_row_text(i, s), classes=cls))
        # Один слот под курсором: подсказка строки ИЛИ confirm удаления — они сменяют
        # друг друга обновлением на месте (_refresh_voice_hint), без пересборки секции.
        confirm = self._voice_confirm_text()
        if confirm:
            frame.mount(
                Static(
                    Text(confirm, style=WARN),
                    classes=f"set-confirm set-dyn set-voicehint {KEEP_IN_VIEW}",
                )
            )
        else:
            # Высота — под длинное из двух описаний режима (дизайн-код §9).
            frame.mount(
                ReservedHint(
                    self._voice_hint_text(s),
                    variants=self._voice_hint_variants,
                    classes="set-hint set-dyn set-voicehint",
                )
            )
        self._mount_voice_fields(frame, s)
        self._voice_mounted_fields = self._voice_fields_key()
        if self._voice_cursor_group() == _VOICE_GROUP_VARIANTS:
            # Строку под курсором подводим после layout-прохода (тот же приём, что у
            # carry-fix'ов рендера); поле в зону видимости Textual тянет сама, за фокусом.
            self.call_after_refresh(self._sync_voice_focus)

    def _mount_voice_fields(self, frame: Vertical, s) -> None:
        """Поля панели ПОД КУРСОРОМ: у «Локального сервиса» — факты его настройки (правит
        их установка, человек правит только язык), у «Своего endpoint» — редактируемые
        поля из config.yml с probe-строкой. Локальному режиму без батарейки показывать
        нечего — вместо полей строка с ценой установки."""
        from gc_tui.editors.voice_endpoint import VoiceEndpointEditor

        if not self._voice_fields_shown():
            frame.mount(
                Static(
                    _VOICE_INSTALL_HINT,
                    classes="set-hint set-dyn set-voiceinstall",
                )
            )
            return
        # Фокусом распоряжается курсор: он на поле — просим редактор встать именно в
        # него на СВОЁМ монтировании (снаружи поля в этот момент ещё не собраны), курсор
        # на строке режима — не забирать фокус вовсе.
        cursor_field = self._voice_cursor_field() or False
        if self._voice_pane == "local":
            from gc_tui.voice_runtime.wire import (
                local_url,
            )  # формат адреса — у владельца wire

            frame.mount(
                VoiceEndpointEditor(
                    values={
                        "url": local_url(s.port),
                        "token": _VOICE_LOCAL_TOKEN,
                        "model": s.model or "",
                        "language": self._voice_setup.local_language(
                            self._settings
                        ),
                    },
                    fields=_VOICE_FIELDS,
                    gap=True,
                    autofocus=cursor_field,
                    readonly=_VOICE_LOCAL_READONLY,
                )
            )
            return
        raw_voice = self._voice_raw_cloud()
        draft = self._voice_cloud_draft
        # Язык — только здесь: мастер его не спрашивает (спека §3), дефолт схемы ru.
        # Токен не префиллим (секрет), но и молчать нельзя — показываем сохранённый
        # placeholder'ом под канонной маской (display_secret «Подключения»).
        from gc_core.onboarding import display_secret

        raw_token = raw_voice.get("token")
        placeholders = (
            {"token": display_secret(raw_token)} if raw_token else None
        )
        values = {
            "url": str(raw_voice.get("url") or ""),
            "token": "",  # секрет — не префиллится
            "model": str(raw_voice.get("model") or ""),
            "language": str(raw_voice.get("language") or "ru"),
        }
        from gc_core.config_modules.voice_endpoint_presets import (
            voice_endpoint_presets_from_config,
        )

        frame.mount(
            VoiceEndpointEditor(
                values=draft if draft is not None else values,
                fields=_VOICE_FIELDS,
                gap=True,
                autofocus=cursor_field,
                placeholders=placeholders,
                # Поле «Модель» здесь — со списком с САМОГО сервера: чем его брать (фасад,
                # как и probe), чем дополнять (сохранённый токен — пустое поле значит «тот
                # же», ровно как при сохранении) и как проверять TLS (voice.verify_ssl).
                fetch=self._voice_setup.list_models,
                saved_token=str(raw_token or ""),
                verify_ssl=self._voice_verify_ssl(),
                config_proxy=self._cfg_settings().backend.proxy or None,
                # Поле «URL» — со списком пресетов адреса (спека 2026-08-05 §3). Только
                # облачная панель: локальная показывает адрес ФАКТОМ (readonly), и
                # VoiceEndpointEditor сам не заводит список у readonly-поля.
                url_presets=voice_endpoint_presets_from_config(self._settings),
                save_hint=True,
            )
        )
        if self._cfg_settings().backend.proxy:
            # Только облачная панель: локальный сервис на этой же машине и идёт мимо
            # прокси — упоминать его там было бы неправдой.
            frame.mount(
                Static(
                    _VOICE_PROXY_HINT,
                    classes="set-hint set-dyn set-voiceproxy",
                )
            )
        frame.mount(
            Static(
                self._voice_probe_line(),
                classes=_CLS_SET_HINT_SET_DYN_SET_PROBE,
            )
        )

    def _voice_verify_ssl(self):
        """voice.verify_ssl ДЕЙСТВУЮЩЕГО конфига (там ключ уже нормализован из
        `verify-ssl` схемой) или True — проверять сертификат по умолчанию."""
        return self._cfg_settings().voice.verify_ssl

    def _voice_fields_shown(self) -> bool:
        """Есть ли что показывать в полях: у локального режима без батарейки — нет."""
        if self._voice_pane != "local":
            return True
        s = self._voice_status
        return bool(s is not None and s.installed)

    def _voice_ribbon_fields(self) -> tuple:
        """Поля, на которых курсор ОСТАНАВЛИВАЕТСЯ (группа полей). В облачной панели —
        все; в локальной только «Язык»: остальные показывают факты сервиса, править их
        нечем, и остановка на них была бы тупиком (disabled-поле фокус всё равно не берёт)."""
        if self._voice_pane == "cloud":
            return _VOICE_FIELDS
        return ("language",) if self._voice_fields_shown() else ()

    def _voice_fields_key(self) -> tuple:
        """Сигнатура НАБОРА полей: пересобирать их надо ровно тогда, когда она меняется
        (сменилась панель либо появилась/исчезла батарейка), и не чаще — в полях может
        лежать набранный черновик."""
        return (self._voice_pane, self._voice_fields_shown())

    def _capture_voice_cloud_draft(self) -> None:
        """Снять набранное в полях endpoint перед их сносом (канон мастера
        `_capture_voice_fields`): черновик принадлежит человеку, а не рендеру, и обязан
        пережить заход на соседнюю строку режима."""
        if (
            self._voice_mounted_fields is None
            or self._voice_mounted_fields[0] != "cloud"
        ):
            return  # в локальной панели полей endpoint нет
        try:
            self._voice_cloud_draft = dict(self._voice_editor().values())
        except (NoMatches, KeyError):
            return

    def _relayout_voice_fields(self) -> bool:
        """Пересобрать секцию, если набор полей изменился. True — пересобрали (строки и
        подсказка при этом уже свежие, доделывать нечего)."""
        if self._voice_fields_key() == self._voice_mounted_fields:
            return False
        for ctrl in self._voice_all_lists():
            # Сворачиваем ДО снимка черновика: в поле может лежать остаток фильтра списка,
            # и черновик не имеет права унести его с собой в пересобранные поля.
            ctrl.close()
        self._capture_voice_cloud_draft()
        if self._voice_pane != "cloud":
            # Вердикт и волна probe — про поля endpoint, которых на экране больше нет;
            # летящую проверку гасим, иначе тик волны смонтировал бы probe-строку в
            # локальную панель (`_set_probe_line` умеет монтировать).
            self._reset_voice_probe()
        if _VOICE_GROUP_FIELDS not in self._voice_groups():
            self._voice_group = (
                _VOICE_GROUP_VARIANTS  # группа полей под курсором исчезла
            )
        self._render_view()
        return True

    def _voice_override_text(self) -> str | None:
        """Строка честности канона `_mount_field_hints` или None (говорить не о чем).

        Адрес в полях — из config.yml, а действовать может другой. Штатное перекрытие
        молчаливо: локальный режим И РАБОТАЕТ тем, что managed-слой ложится поверх
        config.yml — ⚠ на нём кричал бы волком. Говорим, когда перекрытие НЕОЖИДАННОЕ:
        перекрыл не наш managed (проектный .gc/config.yml) либо managed есть, но ведёт
        не на наш локальный сервис. Слой считаем тем же `config_origin.origins`, что и
        поля визарда — voice.* просто не заведены в FIELD_ANCHOR_PATHS, а origins()
        берёт любой dotted-путь."""
        if not self._config_path:
            return None
        try:
            from gc_core.config_origin import origins, overlay_label

            origin = origins(
                self._config_path,
                ["voice.url"],
                profile_override=self._acting_profile(),
            )["voice.url"]
        except Exception:
            return None  # диагностика не обязана ронять секцию
        if not origin.is_overridden or not origin.effective:
            return None
        if origin.overlay_kind == "managed" and self._voice_url_is_ours(
            origin.effective
        ):
            return None
        # Подпись строит `overlay_label`, а не сырой словарь: профильные значения в
        # нём — ШАБЛОНЫ с `{name}`, и прямая подстановка печатала человеку
        # «перекрыто профилем {name} (проектным)». Форматирование живёт в одной
        # точке — там же, где соседние витрины («Подключение», запись реестра).
        # Хвост про режим — не украшение: перекрывший слой бьёт ОБА варианта, и без
        # него человек читает строку как «проблема того ряда, что рядом» и
        # рассчитывает уйти от неё переключением.
        return f"⚠ действует {origin.effective} — перекрыто {overlay_label(origin)}; выбор режима ниже этого не изменит"

    def _voice_url_is_ours(self, url) -> bool:
        """Адрес ведёт на наш локальный сервис. Проверку не дублируем — зовём гейт
        автостарта (единственный её владелец), порт берём из статуса батарейки."""
        s = self._voice_status
        if s is None or s.port is None:
            return False
        from gc_tui.voice_runtime.autostart import voice_url_is_ours

        return voice_url_is_ours(str(url or ""), s.port)

    def _mount_voice_override_hint(self, frame: Vertical) -> None:
        text = self._voice_override_text()
        if text:
            frame.mount(Static(text, classes=_VOICE_OVERRIDE_CLASSES))

    def _refresh_voice_override(self) -> None:
        """Перекрытие меняется вместе со слоями (установка/удаление местного сервиса
        правят managed-оверлей) — строку заводим/обновляем/убираем НА МЕСТЕ, сразу
        ПЕРЕД строками режима; секцию для этого пересобирать не нужно."""
        text = self._voice_override_text()
        # Виджет, УЖЕ помеченный на удаление (`_pruning`), существующим не считаем: он
        # ещё в дереве, но обречён — обновление исчезло бы вместе с ним (канон `_singleton`).
        existing = [
            w for w in self.query(".set-voiceoverride") if not w._pruning
        ]
        if not text:
            for w in existing:
                w.remove()
            return
        if existing:
            existing[-1].update(text)
            return
        rows = self._voice_row_widgets()
        if len(rows) != len(_VOICE_ROWS):
            return
        self.query_one(_SEL_SET_FRAME, Vertical).mount(
            Static(text, classes=_VOICE_OVERRIDE_CLASSES), before=rows[0]
        )

    def _voice_row_text(self, i: int, s) -> Text:
        """Строка режима в языке опций мастера (onboarding._voice_row_text): курсор ▸
        отдельно от пометки (●) — ● стоит на ДЕЙСТВУЮЩЕМ режиме конфига, а не под
        курсором (режим «выключен» — ни одной ●). Приглушённый хвост локальной строки —
        её статус (см. _voice_local_tail).

        ▸ стоит на позиции группы строк режима и когда курсор ушёл в поля: поля идут
        ниже обеих строк, и без метки не видно, к какому режиму они относятся (поймано
        живой приёмкой). Фоновая подсветка при этом снимается — «курсор НА строке» и
        «курсор в её полях» разные состояния, и различать их нечем, кроме неё."""
        active = _VOICE_MODE_ROW.get(s.mode)
        t = Text()
        t.append("▸ " if i == self._voice_row else "  ", style=ACCENT)
        t.append("(")
        t.append("●" if i == active else " ", style=INK)
        t.append(f") {_VOICE_ROWS[i]}", style=INK)
        tail = self._voice_local_tail(s) if i == _VOICE_ROW_LOCAL else ""
        if tail:
            t.append(f"  {tail}", style=DEEP_DIM)
        # Устаревший движок — тем же языком, что у MCP-строки («· доступно 0.3.1»,
        # WARN): один продукт, одна форма для «есть версия новее».
        if i == _VOICE_ROW_LOCAL and s is not None and s.available_version:
            t.append(f" · доступно {s.available_version}", style=WARN)
        return t

    def _voice_local_tail(self, s) -> str:
        """Статус батарейки. Пока её нет, статуса не существует — и хвост пуст: цену с
        именем модели говорит описание режима, повторять их в строке незачем."""
        if s is not None and s.installed:
            # None — живость ещё едет из воркера (первый кадр): честная пауза «…»
            # вместо мгновенного «не запущен», который через полсекунды дёргался бы
            # в «запущен» на живом сервисе.
            if s.service_alive is None:
                alive = "…"
            else:
                alive = "запущен" if s.service_alive else "не запущен"
            return f"установлен · {alive} · порт {s.port}"
        return ""

    def _voice_hint_text(self, s=None) -> str:
        """Описание режима ПОКАЗАННОЙ панели (канон §6: без клавиш — их держит футер).
        Поля — часть своей строки режима, подсказка у них с ней общая."""
        if self._voice_pane != "local":
            return _VOICE_MODE_HINTS["cloud"]
        from gc_tui.voice_runtime.state import current_model_short

        return _VOICE_MODE_HINTS["local"].format(model=current_model_short())

    @staticmethod
    def _voice_hint_variants() -> list[str]:
        """Описания обоих режимов — резерв высоты подсказки голоса."""
        from gc_tui.voice_runtime.state import current_model_short

        local = _VOICE_MODE_HINTS["local"].format(model=current_model_short())
        return [local, _VOICE_MODE_HINTS["cloud"]]

    def _voice_row_widgets(self) -> list:
        """Строки режима текущего рендера: последние len(_VOICE_ROWS) матчей — старые
        строки ещё не отсоединены (`remove()` отложенный, приём `.set-modes-hint`)."""
        return list(self.query(".set-voicerow"))[-len(_VOICE_ROWS) :]

    def _refresh_voice_section(self, *, kick_alive: bool = True) -> None:
        """Обновить секцию по свежему статусу. Поля пересобираем ТОЛЬКО когда изменился
        их набор (появилась/исчезла батарейка): во всех остальных случаях (вердикт
        подъёма, смена режима) в них может лежать набранный черновик.

        kick_alive=False — только для пути «живость приехала из воркера»
        (_on_voice_alive): пере-замер оттуда закольцевал бы пробу саму на себя."""
        if kick_alive:
            self._kick_voice_alive()  # действия меняют сервис — старая живость протухла
        self._voice_status = self._voice_snapshot(force=True)
        if self._relayout_voice_fields():
            return
        self._refresh_voice_override()
        self._refresh_voice_rows()
        self._refresh_voice_hint()

    def _refresh_voice_rows(self) -> None:
        """Перерисовать строки режима НА МЕСТЕ (канон мастера `_move_voice`): полный
        `_render_view` поднял бы поля из файла заново и стёр набранный черновик."""
        rows = self._voice_row_widgets()
        s = self._voice_status
        if len(rows) != len(_VOICE_ROWS) or s is None:
            return
        highlight = self._voice_highlight_variants()
        for i, w in enumerate(rows):
            w.update(self._voice_row_text(i, s))
            # Курсор в полях — ни одной подсвеченной строки (▸ при этом остаётся у
            # владельца, текст его рисует сам).
            (
                w.add_class
                if i == self._voice_row and highlight
                else w.remove_class
            )("selected")

    def _voice_confirm_text(self) -> str:
        """Текст висящего подтверждения (снос или установка) или пустая строка."""
        if self._voice_awaiting_remove:
            return self._VOICE_REMOVE_CONFIRM
        if self._voice_awaiting_install:
            return self._VOICE_INSTALL_CONFIRM
        return ""

    def _voice_awaiting_confirm(self) -> bool:
        return self._voice_awaiting_remove or self._voice_awaiting_install

    def _cancel_voice_confirm(self) -> None:
        """Снять любое висящее подтверждение (навигация, клик, Esc)."""
        self._voice_awaiting_remove = False
        self._voice_awaiting_install = False

    def _refresh_voice_hint(self) -> None:
        """Слот под строками: подсказка курсора ↔ confirm удаления, обновлением на месте."""
        widgets = list(self.query(".set-voicehint"))
        if not widgets:
            return
        w = widgets[-1]
        confirm = self._voice_confirm_text()
        if confirm:
            w.update(Text(confirm, style=WARN))
            w.set_classes(f"set-confirm set-dyn set-voicehint {KEEP_IN_VIEW}")
            # Тело секции не пересобирается — `reveal_focus` никто не позовёт, а
            # вопрос садится ниже вьюпорта (тело длиннее рамки, и кадр мог уехать
            # плашкой итога). Подводим разово сами, как `_singleton`: приоритет
            # держит маркер выше, подводку заказывает тот, кто меняет виджет.
            reveal(w)
        else:
            w.update(self._voice_hint_text())
            w.set_classes("set-hint set-dyn set-voicehint")

    # --- группы курсора секции (спека §4, канон `OnboardingScreen`) ---
    def _voice_groups(self) -> tuple:
        """Группы секции: строки режима — всегда, поля показанной панели — пока они есть
        (у локального режима без батарейки полей нет вовсе, см. `_voice_ribbon_fields`)."""
        if self._voice_ribbon_fields():
            return (_VOICE_GROUP_VARIANTS, _VOICE_GROUP_FIELDS)
        return (_VOICE_GROUP_VARIANTS,)

    def _voice_cursor_group(self) -> str:
        """Где сейчас курсор: "variants" или "fields". Одно место правды — иначе
        состояние приходится выводить из побочных признаков (фокус, видимость полей), а
        они врут при клике мышью и при пустой группе. Хранимая группа сверяется с
        наличными: поля исчезают вместе со сносом батарейки под локальной строкой."""
        groups = self._voice_groups()
        return (
            self._voice_group
            if self._voice_group in groups
            else _VOICE_GROUP_VARIANTS
        )

    def _voice_field_position(self) -> int:
        """Позиция в группе полей, зажатая в её границы (спека §4.2.1): набор полей
        зависит от строки режима под курсором (у cloud их четыре, у local одно), и
        запомненная позиция после переключения может оказаться за краем."""
        n = len(self._voice_ribbon_fields())
        return min(max(self._voice_field_idx, 0), n - 1) if n else 0

    def _voice_highlight_variants(self) -> bool:
        """Красить ли подложкой строку режима. Курсор один (спека §4.1): ушёл в поля —
        подсветки нет ни у одной строки, иначе на экране два курсора сразу, строка и
        фокус-рамка поля. ▸ этому правилу не подчиняется — он маркер ВЛАДЕЛЬЦА
        показанных полей (см. `_voice_row_text`)."""
        return self._voice_cursor_group() == _VOICE_GROUP_VARIANTS

    def _voice_cursor_field(self) -> str | None:
        """Ключ поля показанной панели под курсором или None (курсор в строках режима) —
        местозависимые клавиши/футер/списки различают места по нему."""
        fields = self._voice_ribbon_fields()
        if not fields or self._voice_cursor_group() != _VOICE_GROUP_FIELDS:
            return None
        return fields[self._voice_field_position()]

    def _set_voice_cursor(self, *, group: str, position: int) -> None:
        """Поставить курсор в заданное место и свести с ним всё остальное (подсветку,
        фокус, списки, футер). Заведён для тестов: на поле со списком курсор приходит
        сам раскрытым списком, и пройти СКВОЗЬ такое поле стрелками нельзя (спека
        §4.2.3) — дальние поля иначе недостижимы. Тем же приёмом переносит курсор и
        `_voice_cloud_save` на ошибке сабмита — прыжок туда тоже не идёт стрелками."""
        self._voice_group = group
        if group == _VOICE_GROUP_FIELDS:
            self._voice_field_idx = position
        else:
            self._voice_row = position
        self._refresh_voice_rows()
        self._sync_voice_focus()

    def _voice_token_is_mask(self) -> bool:
        """Под полем «Токен» лежит сохранённый секрет — по этому признаку футер обещает
        Ctrl+U (канон `_connection_can_clear_token` соседней секции)."""
        try:
            return self._voice_editor().token_is_mask()
        except NoMatches:
            return False

    def _voice_clear_token(self) -> bool:
        """Ctrl+U в полях голоса: стереть сохранённый токен. False — клавиша не наша
        (курсор не на «Токене» либо стирать нечего), и её отдают полю ввода."""
        try:
            editor = self._voice_editor()
        except NoMatches:
            return False
        return editor.clear_token(self._voice_cursor_field())

    def _voice_list_ctrl(self):
        """Контроллер списка ПОЛЯ ПОД КУРСОРОМ или None (у поля списка нет / редактора
        нет). Списков в панели три — языка, моделей и адреса (спека 2026-08-05 §3), —
        но раскрытым может быть только тот, на чьём поле стоит курсор (см.
        `_sync_voice_field_lists`)."""
        try:
            return self._voice_editor().list_for(self._voice_cursor_field())
        except NoMatches:
            return None

    def _voice_all_lists(self) -> list:
        try:
            ed = self._voice_editor()
        except NoMatches:
            return []
        return [
            c
            for c in (ed.lang_list, ed.model_list, ed.url_list)
            if c is not None
        ]

    def _sync_voice_field_lists(self, ed=None) -> None:
        """Список поля раскрыт ровно тогда, когда курсор ленты стоит на этом поле (канон
        пресет-листа: «вход на поле — всегда раскрыть»); уход с поля и из секции его
        сворачивает, и соседний список раскрытым остаться не может. Второй раз сам не
        раскрывается: свернули Esc'ом — курсор всё ещё на поле, и `open()` здесь уже не
        зовут до следующего входа (раскрыть тогда можно печатью либо Alt+↓, спека
        2026-08-06 §2, §4.1, см. on_key/VoiceEndpointEditor.on_input_changed)."""
        if ed is None:
            try:
                ed = self._voice_editor()
            except NoMatches:
                return
        active = ed.list_for(self._voice_cursor_field())
        for ctrl in (ed.lang_list, ed.model_list, ed.url_list):
            if ctrl is not None and ctrl is not active:
                # Строку под полем снимать не надо: с канона 2026-08-17 она функция
                # состояния списка и живёт постоянно («вот чем список раскрывается»),
                # а не команда момента — её приводит в согласие сам `close()`.
                ctrl.close()
        if active is None:
            return
        if self._voice_list_reopen_gate:
            # Сохранение облачной панели пересобирает поля, и редактор автофокусирует
            # поле курсора — то есть это выглядит как новый вход на поле. Раскрывать
            # список поверх только что сохранённого выбора незачем (у моделей это ещё и
            # лишний поход в сеть): гейт одноразовый, следующий вход на поле раскроет
            # список как обычно (в локальной панели полей не пересобирают, и там список
            # после Enter остаётся свёрнутым сам).
            self._voice_list_reopen_gate = False
            return
        active.open()

    def _move_voice(self, delta: int) -> None:
        """↑/↓ внутри группы под курсором, циклом (канон и этого файла — меню/«Режимы»/
        «Обновление», и ConnectionEditor.move). Приоритет жёсткий (спека §4.2.2):
        раскрытый список → поля → строки режима.

        Раскрытый список поля клавиши забирает себе (канон пресет-листа: `is_open`
        проверяется первым) — курсор не может «утечь» с поля, пока список не свёрнут
        явно: Enter — подставить, Esc — свернуть."""
        ctrl = self._voice_list_ctrl()
        if ctrl is not None and ctrl.move(delta):
            return
        if self._voice_cursor_group() == _VOICE_GROUP_FIELDS:
            self._move_voice_field(delta)
            return
        self._voice_row = (self._voice_row + delta) % len(_VOICE_ROWS)
        self._cancel_voice_confirm()  # навигация отменяет незавершённый confirm
        # Панель полей задаёт строка режима, на которую встал курсор; уход в поля её не
        # меняет — поля принадлежат той строке, с которой в них вошли.
        self._voice_pane = (
            "local" if self._voice_row == _VOICE_ROW_LOCAL else "cloud"
        )
        if self._relayout_voice_fields():
            return
        self._refresh_voice_rows()
        self._refresh_voice_hint()
        self._refresh_footer()  # клавиши места меняются вместе с курсором
        self._sync_voice_focus()

    def _move_voice_field(self, delta: int) -> None:
        """↑/↓ внутри группы полей, циклом. Раскрытый список забирает стрелки раньше —
        он выше по приоритету (спека §4.2.2), см. `_move_voice`."""
        fields = self._voice_ribbon_fields()
        if not fields:
            return
        self._voice_field_idx = (self._voice_field_position() + delta) % len(
            fields
        )
        self._cancel_voice_confirm()  # навигация отменяет незавершённый confirm
        self._refresh_voice_hint()
        self._sync_voice_focus()

    def _move_group(self, delta: int) -> None:
        """Tab/Shift+Tab — курсор в соседнюю группу, по кругу (спека §4.1). Групп меньше
        двух — переходить некуда, и клавиша просто съедена (§4.2.6). Позиция группы, из
        которой уходим, остаётся при ней: возврат приводит курсор на прежнее место, а
        `_voice_field_position` зажимает его, если полей стало меньше (§4.2.1).

        Раскрытый список сворачивается сам (§4.2.4): его держит `_sync_voice_field_lists`
        по полю ПОД КУРСОРОМ, а курсор из полей ушёл."""
        groups = self._voice_groups()
        if len(groups) < 2:
            return
        cur = groups.index(self._voice_cursor_group())
        self._voice_group = groups[(cur + delta) % len(groups)]
        self._cancel_voice_confirm()  # навигация отменяет незавершённый confirm
        self._refresh_voice_rows()  # курсор сменил группу — подсветка уходит за ним
        self._refresh_voice_hint()
        self._sync_voice_focus()

    def _sync_voice_focus(self) -> None:
        """Связка «курсор ↔ фокус» (канон ConnectionEditor.move): курсор на поле — фокус
        в этом поле (своего маркера у полей нет, индикатор — их фокус-рамка); курсор на
        строке режима — фокус снят, иначе печать и Del достались бы полю, а не секции."""
        if self._view != _VOICE:
            return
        try:
            ed = self._voice_editor()
        except NoMatches:
            ed = None
        key = self._voice_cursor_field()
        if ed is not None and key is not None:
            try:
                # поле в зону видимости Textual подтянет сама, вслед за фокусом
                ed.focus_field(key)
            except NoMatches:
                return  # редактор ещё не собран — фокус поставит его on_mount
        else:
            self.set_focus(None)
            # Фокуса нет — нет и авто-прокрутки: секция с полями выше экрана, и строку
            # под курсором подводим сами (канон списковых экранов: call_after_refresh
            # + scroll_visible после layout-прохода).
            rows = self._voice_row_widgets()
            if len(rows) == len(_VOICE_ROWS) and self._voice_row < len(rows):
                self.call_after_refresh(
                    rows[self._voice_row].scroll_visible, animate=False
                )
        self._sync_voice_field_lists(ed)
        self._refresh_footer()  # список меняет клавиши места — футер после него

    def _voice_focus_in_editor(self, widget=None) -> bool:
        """Фокус (или переданный виджет) внутри полей панели — человек печатает:
        секция не перехватывает его клавиши правки."""
        node = widget if widget is not None else self.focused
        if node is None:
            return False
        try:
            ed = self._voice_editor()
        except NoMatches:
            return False
        return ed in node.ancestors_with_self

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        """Клик мышью в поле — это тоже перемещение курсора (спека §4.3): переносим его
        в группу полей, на кликнутое (иначе следующая стрелка прыгнула бы из «прошлого»
        места), и гасим незавершённый confirm удаления — Enter в поле теперь про
        сохранение, а не про снос."""
        if self._dirty_exit_confirm:
            self.set_focus(None)
            return
        if self._view != _VOICE or not self._voice_focus_in_editor(
            event.widget
        ):
            return
        pos = self._voice_field_index(event.widget)
        if pos is not None and (pos, _VOICE_GROUP_FIELDS) != (
            self._voice_field_idx,
            self._voice_cursor_group(),
        ):
            self._voice_group = _VOICE_GROUP_FIELDS
            self._voice_field_idx = pos
            self._refresh_voice_rows()  # подсветка гаснет: курсор теперь в поле
        self._cancel_voice_confirm()
        self._refresh_voice_hint()
        # Клик мышью — тот же вход на поле, что и стрелкой: список поля подчиняется
        # курсору, где бы тот ни оказался (и он же раскрывает список на монтировании
        # полей — редактор фокусирует своё поле сам).
        self._sync_voice_field_lists()
        self._refresh_footer()

    def _voice_field_index(self, widget) -> int | None:
        """Позиция поля в группе полей по id виджета (или None — на этом поле курсор не
        останавливается)."""
        from gc_tui.editors.voice_endpoint import _FIELDS

        for i, key in enumerate(self._voice_ribbon_fields()):
            if getattr(widget, "id", None) == _FIELDS[key][1]:
                return i
        return None

    def _suspended(self, do, banner: str) -> bool:
        """Действие с выводом CLI в обычный терминал: под `app.suspend()` TUI уходит
        с alt-screen, и прогресс `gc-voice …` видно как в терминале (канон установки
        MCP в мастере). Драйвер может suspend не поддерживать — тогда ОДИН fallback,
        мимо alt-screen.

        Три исхода разведены намеренно (находка ревью коллеги: прежний код различал
        только «успели ли доделать», и упавшее ДЕЙСТВИЕ запускалось вторично — для
        установки это второй clone/uv sync/download):
        * не вышло войти в suspend — действие ещё не начинали, повторяем без него;
        * упало само действие — повтор запрещён, отвечаем «не вышло»;
        * упал выход из suspend уже ПОСЛЕ действия — отдаём его результат."""
        started = finished = False
        ok = False
        try:
            with self.app.suspend():
                print(banner)
                started = True
                ok = do()
                finished = True
        except Exception:
            if finished:
                return ok  # операция сделана, споткнулся только выход
            if started:
                return False  # упало действие — второй раз не заводим
            try:
                ok = do()  # suspend недоступен: тот же вызов, без экрана
            except Exception:
                return False
        return ok

    def _voice_hot_reload(self) -> bool:
        """Донести правку голоса до живого приложения (канон `_do_save`): `_on_apply` —
        это `app.reload_config`. Перечитанный конфиг забираем себе: и статус секции, и
        подъём сервиса должны смотреть на новое состояние, а не на снимок при открытии."""
        try:
            self._on_apply()
        except Exception:
            return False
        self._pull_base_from_app()
        # Конфиг перечитан — кэш снимка обесценен: он снят по прежним слоям.
        self._invalidate_voice_snapshot()
        return True

    def _start_voice_service(self) -> None:
        """Поднять локальный сервис по УЖЕ перечитанному конфигу (установка только что
        переписала config.local.yml — гейт автостарта смотрит именно туда), ФОНОМ.

        Синхронно нельзя: после свежей установки порт заведомо мёртв, и
        `ensure_voice_service` спавнит процесс и блокирующе ждёт готовности (до 90 с —
        холодный старт грузит окружение и модель) — UI замер бы уже ПОСЛЕ выхода из
        suspend, без единого признака жизни. Паттерн
        тот же, что у probe: воркер + `call_from_thread` (виджеты трогаем только на
        UI-потоке). Сбой подъёма экран не роняет."""
        cfg = self._settings

        def _work() -> None:
            from gc_tui.voice_runtime.autostart import ensure_voice_service

            try:
                ok = ensure_voice_service(cfg)
            except Exception:
                ok = False
            try:
                self.app.call_from_thread(self._on_voice_service_started, ok)
            except Exception:
                # Полёт длится до 90 с; за это время приложение могло закрыться —
                # и `self.app` на отцепленном экране бросает. Обёртка как у соседей
                # (app.py, voice_endpoint, onboarding): вердикт некому показывать.
                pass

        self.run_worker(_work, thread=True, name="settings-voice-start")

    def _on_voice_service_started(self, ok: bool) -> None:
        """Вердикт подъёма на UI-потоке — с гейтом состояния, как у всех воркер-колбэков
        файла (`_on_config_probe`, `_on_draft_probe`, `_apply` у `_voice_probe`).

        Полёт длится до 90 с, и за это время юзер мог уйти куда угодно. В ЧУЖОЙ секции
        трогать фрейм нельзя вовсе (`_render_view` там же делает `set_focus(None)` и
        потерял бы недосохранённое соседа), под висящим confirm — тоже (снесли бы баннер
        из-под руки): туда итог доносим toast'ом приложения, он фрейм не трогает.

        В своей секции обновляем ТОЧЕЧНО (`_refresh_voice_section`). Полного
        `_render_view()` здесь не бывает по построению: он пересоздал бы поля endpoint'а,
        а в них может лежать черновик, набранный за эти 90 секунд (фокус в полях для
        этого держать не обязательно — курсор мог уйти на строку выше)."""
        if not self.is_mounted:
            return  # экран закрыли, пока сервис поднимался
        self._voice_alive = bool(
            ok
        )  # вердикт подъёма и есть живость — не ждём пробу
        self._invalidate_voice_snapshot()
        text, outcome = (
            ("локальный сервис подключён", "ok")
            if ok
            else (
                "локальный сервис подключён, но не запустился — проверьте gc-voice status",
                "warn",
            )
        )
        if self._view != _VOICE or self._voice_awaiting_confirm():
            # Секцию не трогаем (в ней либо не мы, либо висит confirm), но сервис только
            # что сменил состояние — кэш снимка обесценен для следующего читателя.
            self._invalidate_voice_snapshot()
            self.app.notify(text, severity="information" if ok else "warning")
            return
        self._refresh_voice_section()  # статус секции — по факту (сервис жив/нет)
        self._flash(
            text, outcome=outcome
        )  # _singleton обновит плашку «поднимаю сервис…»

    def _voice_enter(self) -> None:
        """Enter выбирает/раскрывает список; на локальной строке включает сервис."""
        if self._voice_pick_from_list():
            return
        ctrl = self._voice_list_ctrl()
        if ctrl is not None:
            ctrl.reload()
            self._refresh_footer()
            return
        if (
            self._voice_pane == "local"
            and self._voice_cursor_group() == _VOICE_GROUP_VARIANTS
        ):
            self._voice_enable_local()

    def _voice_is_dirty(self) -> bool:
        raw = self._voice_raw_cloud()
        original = {
            "url": str(raw.get("url") or ""),
            "token": "",
            "model": str(raw.get("model") or ""),
            "language": str(raw.get("language") or "ru"),
        }
        if (
            self._voice_cloud_draft is not None
            and self._voice_cloud_draft != original
        ):
            return True
        try:
            editor = self._voice_editor()
        except NoMatches:
            return False
        values = editor.values()
        if self._voice_pane == "cloud":
            return values != original or editor.token_cleared
        return values.get(
            "language", "ru"
        ) != self._voice_setup.local_language(self._settings)

    def _voice_pick_from_list(self) -> bool:
        """Подставить подсвеченную строку списка ПОД КУРСОРОМ в его поле и свернуть
        список. Списка нет или он свёрнут — не делаем ничего; раскрыт, но подсвеченной
        строки в нём нет (в поле своё значение) — только сворачиваем: сохранится
        введённое. True — список был раскрыт (и этим Enter'ом свёрнут)."""
        key = self._voice_cursor_field()
        ctrl = self._voice_list_ctrl()
        if ctrl is None or not ctrl.is_open:
            return False
        try:
            self._voice_editor().pick_in(key)
        except NoMatches:
            return False
        # Подстановка — такая же правка поля, как печать, но `Input.Changed` она не
        # рождает (`_write_field` пишет через `set_reactive`), и вердикт probe сам бы
        # не погас: зелёная ✓ осталась бы про ПРОШЛУЮ модель, а сохранение следом
        # перенесло бы эталон сравнения уже на новую — ✓ врала бы до конца сеанса.
        self._voice_values_changed()
        self._refresh_footer()  # список свернулся — футер снова про место
        return True

    def _voice_language_to_save(self, raw: str) -> str | None:
        """Код языка для записи или None — код не годится (сообщили и не сохраняем).
        Пусто — дефолт схемы `ru` (как было до списка)."""
        from gc_tui.editors.voice_endpoint import normalized_language

        language = normalized_language(raw or "ru")
        if language is None:
            self._flash(_VOICE_LANG_ERROR, outcome="warn")
        return language

    def _voice_local_language_save(self) -> None:
        """Enter на «Языке» локального режима: язык живёт в managed-слое (его пишем мы,
        не юзер) — дальше тот же hot-reload и та же плашка, что у облачного сохранения."""
        values = self._voice_field_values()
        language = self._voice_language_to_save(values[3] if values else "")
        if language is None:
            return
        if not self._voice_setup.set_local_language(language):
            self._flash("не сохранено: локального сервиса нет", outcome="warn")
            return
        reloaded = self._voice_hot_reload()
        self._close_or_back()
        if reloaded:
            self._flash(f"язык распознавания: {language}")
        else:
            self._flash(f"сохранено, {_MSG_APPLIES_NEXT_START}")

    def _voice_enable_local(self) -> None:
        """Enter на «Локальном сервисе»: нет батарейки — поставить (и включить), есть, но
        режим чужой — включить, режим уже локальный — поднять лежащий сервис (или сказать,
        что всё и так работает).

        Ветка «локальный, но не запущен» обязательна: переключать там нечего, и без неё
        строка была бы тупиком — поднять сервис из UI больше нечем."""
        s = self._voice_status or self._voice_snapshot()
        if s.mode == "local":
            if s.service_alive:
                self._title_note("уже включён")
                return
            self._flash("поднимаю сервис… (первый запуск — до полутора минут)")
            self._start_voice_service()
            return
        installed = s.installed
        if not installed:
            # Установка качает ~0.9 ГБ и уводит экран в терминал на минуты — спрашиваем,
            # как и перед сносом (канон confirm секции). Сама установка — по Enter'у
            # подтверждения, см. _voice_confirm_install.
            self._voice_awaiting_install = True
            self._refresh_voice_hint()
            return
        self._voice_install_local(installed=True)

    def _voice_confirm_install(self) -> None:
        """Enter по висящему подтверждению установки."""
        self._voice_awaiting_install = False
        self._voice_install_local(installed=False)

    def _voice_install_local(self, *, installed: bool) -> None:
        """Перевод режима на локальный сервис: с установкой (батарейки нет) или без неё."""

        # Действующий конфиг — фолбэк языка: managed-секции может уже не быть
        # (переезд на свой endpoint её сносит), а выбор человека терять нельзя.
        def _switch() -> bool:
            return self._voice_setup.switch_to_local(settings=self._settings)

        if installed:
            # Батарейка на месте — это просто wire (запись секции в config.local.yml):
            # мгновенно и молча. Выход в терминал тут гнал бы alt-screen туда-обратно
            # на каждое переключение режима (поймано живой приёмкой).
            try:
                ok = _switch()
            except Exception:
                ok = False
        else:
            # Установка шумная (клон, uv sync, модель) — её вывод нужен в терминале.
            ok = self._suspended(
                _switch, "\nУстанавливаю локальный голосовой сервис …\n"
            )
        if ok:
            # Порядок: reload (быстрый, синхронно) → подъём (воркером): гейт
            # автостарта обязан читать уже перечитанный конфиг.
            self._voice_hot_reload()
        # Точечно, не `_render_view()`: установка меняет статус и слои конфига, но не
        # содержимое полей — а в них может лежать недосохранённый endpoint.
        self._refresh_voice_section()
        if not ok:
            self._flash(
                "включить не удалось" if installed else "установка не удалась",
                outcome="warn",
            )
            return
        self._flash(
            "локальный сервис подключён · поднимаю сервис… (первый запуск — до полутора минут)"
        )
        self._start_voice_service()

    def _voice_update_engine(self) -> None:
        """Ctrl+U на «Локальном сервисе»: обновить движок до версии этой сборки агента.

        Гейт — тот же, что у Del (`_voice_request_remove`): строка режима, не поля, и
        только когда обновлять правда есть что. Подтверждения нет: в отличие от
        удаления, операция ничего не теряет — переустановка того же сервиса на месте.

        Ход показываем в терминале под `_suspended`, как установку и удаление рядом:
        обновление такое же шумное (uv sync, модель), и своего места под прогресс в
        секции нет."""
        if (
            self._voice_cursor_group() != _VOICE_GROUP_VARIANTS
            or self._voice_row != _VOICE_ROW_LOCAL
            or self._voice_awaiting_confirm()
        ):
            return
        s = self._voice_status
        if s is None or not s.available_version:
            return
        ok = self._suspended(
            lambda: self._voice_setup.update_local(self._settings, echo=print),
            f"\nОбновляю голосовой движок до {s.available_version} …\n",
        )
        if ok:
            # Переустановка переписывает managed-слой (тот же порт и модель) — конфиг
            # перечитываем по тому же порядку, что при установке: сначала reload, потом
            # обновление секции по свежему статусу.
            self._voice_hot_reload()
        self._refresh_voice_section()
        self._flash(
            "движок обновлён" if ok else "обновить не удалось",
            outcome="ok" if ok else "warn",
        )

    def _voice_request_remove(self) -> None:
        """Del на «Локальном сервисе» (канон удаления списковых экранов — /sessions):
        сначала подтверждение. На «Своём endpoint» и без батарейки — no-op."""
        if (
            self._voice_cursor_group() != _VOICE_GROUP_VARIANTS
            or self._voice_row != _VOICE_ROW_LOCAL
            or self._voice_awaiting_confirm()
        ):
            return
        s = self._voice_status
        if s is None or not s.installed:
            return
        self._voice_awaiting_remove = True
        self._refresh_voice_hint()

    def _voice_confirm_remove(self) -> None:
        self._voice_awaiting_remove = False
        ok = self._suspended(
            self._voice_setup.remove_local,
            "\nУдаляю локальный голосовой сервис …\n",
        )
        if ok:
            self._voice_hot_reload()
        self._refresh_voice_section()  # без пересборки полей, как при установке
        self._flash(
            "локальный сервис удалён" if ok else "удалить не удалось",
            outcome="ok" if ok else "warn",
        )

    def _reset_voice_probe(self) -> None:
        """Уход из секции обесценивает probe: вердикт снимаем (повторный вход не должен
        показывать ✓ от прошлых, возможно уже других, значений), «в полёте» гасим (иначе
        тик волны монтировал бы probe-строку в секцию, где её уже нет), generation
        бампаем — стухший воркер не ляжет на свежие поля. Смысл тот же, что у
        `_reset_draft_state` в редакторных секциях."""
        self._voice_probe_result = None
        self._voice_probe_in_flight = False
        self._voice_probe_gen += 1
        self._voice_probe_values = None

    def _voice_probe_line(self) -> Text:
        r = self._voice_probe_result
        t = Text()
        if self._voice_probe_in_flight:
            # Та же волна, что у server-probe «Подключения» (спека §4): «в полёте»
            # показывает живой индикатор, а не статичную фразу. Кадр гонит общий
            # _wave_tick — своего таймера секции не заводим.
            return self._wave_glyph()
        if r is None:
            # Канон probe-строки «Подключения»: состояние, а не обещание клавиши —
            # подсказки про клавиши живут в футере (спека §4.3, гейт §11.6).
            t.append("— не проверено", style=DEEP_DIM)
        elif r.ok:
            t.append(f"✓ endpoint отвечает · {r.latency_ms} мс", style=OK)
        else:
            t.append(f"✗ {r.error}", style=WARN)
        return t

    def _voice_editor(self):
        """Свежесмонтированный редактор: после `_render_view` старый ещё в дереве
        (`remove()` отложенный), и `query_one` отдал бы именно его — обречённый
        (канон «последние N матчей» этого файла)."""
        from gc_tui.editors.voice_endpoint import VoiceEndpointEditor

        editors = list(self.query(VoiceEndpointEditor))
        if not editors:
            raise NoMatches("VoiceEndpointEditor не смонтирован")
        return editors[-1]

    def _voice_field_values(self) -> tuple | None:
        """(url, token, model, language) полей или None (редактора нет/не собран)."""
        try:
            vals = self._voice_editor().values()
        except (NoMatches, KeyError):
            return None
        return (vals["url"], vals["token"], vals["model"], vals["language"])

    def _voice_values_changed(self) -> None:
        """Правка полей обесценивает вердикт probe (канон `_reset_draft_state`).

        Сравниваем ЗНАЧЕНИЯ, а не факт события: `_render_view` пересоздаёт поля, и их
        монтирование само рождает Input.Changed — по нему вердикт гас бы на ровном месте
        (в т.ч. сразу после сохранения, где он честен)."""
        if self._voice_probe_values is None:
            return
        values = self._voice_field_values()
        if values is None or values == self._voice_probe_values:
            return
        self._voice_probe_result = None
        self._voice_probe_in_flight = False
        self._voice_probe_gen += (
            1  # летящая проверка — уже про другие значения
        )
        self._voice_probe_values = None
        self._set_probe_line(self._voice_probe_line())

    def _voice_probe(self) -> None:
        if self._voice_pane != "cloud":
            return  # локальная панель адреса не спрашивает — проверять нечего
        values = self._voice_field_values()
        if values is None:
            return
        url, model = values[0], values[2] or "whisper-1"
        # Пустое поле токена = «тот же, что сохранён» — то же правило, что у
        # _voice_cloud_save: проверяем ровно ту связку, которую сохранит Enter. Иначе
        # проверка уходила бы на сервер без авторизации и отвечала 401 на рабочем ключе.
        token = values[1] or str(self._voice_raw_cloud().get("token") or "")
        if not url:
            # Канон draft-probe «Подключения»: молчать в ответ на клавишу нельзя —
            # объясняем, чего не хватает, прямо в строке probe. Базовая фраза общая
            # со списком моделей endpoint'а (`voice_endpoint._URL_REQUIRED`,
            # `ModelListController._load`) — находка финального ревью 2026-08-06 (F):
            # разные ответы на один и тот же вопрос «адреса нет» путали; хвост
            # «проверка стучится на него» — только здесь, это специфика пробы.
            self._show_probe_line(
                Text(
                    f"{_URL_REQUIRED} — проверка стучится на него",
                    style=DEEP_DIM,
                )
            )
            return
        from gc_core import config_raw
        from gc_core.onboarding import _valid_endpoint_url

        if not _valid_endpoint_url(url) and not config_raw.env_refs(url):
            # §2.3 спеки 2026-08-06: пока список URL раскрыт, в поле может лежать
            # ФИЛЬТР, а не адрес — бить пробой по нему вслепую нечестно (та же поправка,
            # что у «Подключения»). Единственный вызывающий этой функции — ручной
            # Ctrl+Space, так что проверка здесь ничьей ещё семантики не меняет.
            #
            # Ссылка ${ENV:VAR}/${FILE:...} — штатное СЫРОЕ значение поля (спека
            # умных форм), не фильтр: `_valid_endpoint_url` её тоже бракует (нет
            # http(s)-схемы), но блокировать пробу здесь нельзя — это откроет наружу
            # ту же дыру, что чинили в «Подключении» (фикс-раунд 1 ревью): корп-конфиг
            # с ${ENV:GC_URL} потерял бы ручную перепроверку связи насовсем.
            self._show_probe_line(
                Text(
                    "сначала выберите адрес endpoint — проверка стучится на него",
                    style=DEEP_DIM,
                )
            )
            return
        self._voice_probe_values = values
        self._voice_probe_gen += 1
        gen = self._voice_probe_gen
        self._voice_probe_in_flight = True
        self._start_wave()  # общий таймер анимации (стоп — в _wave_tick)
        # Точечное обновление строки probe, БЕЗ _render_view(): полный ре-рендер секции
        # заново поднял бы поля из файла (_mount_voice_section) и стёр бы то, что человек
        # только что набрал (тот же приём и та же причина, что у _cancel_env_confirm).
        # Единственный вызывающий — ручной Ctrl+Space: строка — ответ на клавишу.
        self._show_probe_line(self._voice_probe_line())
        # Всё, что нужно запросу, снимаем ЗДЕСЬ: поток в состояние экрана не лезет.
        verify_ssl = self._voice_verify_ssl()

        def _work() -> None:
            r = self._voice_setup.probe_endpoint(
                url,
                token,
                model,
                config_proxy=self._cfg_settings().backend.proxy or None,
                verify=verify_ssl,
            )

            def _apply() -> None:
                if (
                    gen != self._voice_probe_gen
                    or not self.is_mounted
                    or self._view != _VOICE
                ):
                    return  # устаревший/посмертный — отбросить
                self._voice_probe_in_flight = False
                self._voice_probe_result = r
                self._show_probe_line(self._voice_probe_line())

            try:
                self.app.call_from_thread(_apply)
            except Exception:
                pass  # приложение уже закрылось — некому показывать результат

        self.run_worker(_work, thread=True, name="settings-voice-probe")

    def _voice_cloud_save(self) -> None:
        """Сохранение полей — это и есть включение режима «свой endpoint»."""
        editor = self._voice_editor()
        ok, vals = editor.submit()
        if not ok:
            # Ошибка — внутри редактора (канон EndpointEditor), а курсор ленты и фокус
            # едут на ПРОВИНИВШЕЕСЯ поле (submit() называет его в `last_error_field`):
            # пустая «Модель» на чужом пресете URL иначе уводила бы прочь от поля,
            # названного в самой ошибке (находка финального ревью 2026-08-05).
            # Пересборки полей не будет (сохранение не вышло) — некому погасить гейт
            # одноразового не-раскрытия списка, а он мог остаться взведённым с этого
            # же Enter'а (подстановка пресета перед попыткой сохранить); гасим ДО
            # переноса курсора — иначе он молча съест честное автораскрытие списка на
            # только что названном поле.
            self._voice_list_reopen_gate = False
            field = editor.last_error_field or "url"
            self._set_voice_cursor(
                group=_VOICE_GROUP_FIELDS,
                position=self._voice_ribbon_fields().index(field),
            )
            return
        url, token = vals["url"], vals["token"]
        model = vals["model"]
        language = self._voice_language_to_save(vals["language"])
        if language is None:
            return  # мусор в поле «Язык» в config.yml юзера не уезжает
        if not token and not editor.token_cleared:
            # Пусто = «не менять» (канон «Подключения»: поле токена — секрет, не
            # префиллено; пустое при сохранении не должно затирать уже сохранённый).
            # Исключение — Ctrl+U: там пустота осознанная, и сохранённый уходит из файла.
            token = str(self._voice_raw_cloud().get("token") or "")
        # Свой endpoint — единственная запись голоса в ЮЗЕРСКИЙ config.yml (язык и режим
        # живут в managed-слое), поэтому guard пакетного шаблона нужен ровно здесь и
        # ровно тот же, что у остальных секций файла.
        if not self._writable():
            self._flash(
                "не сохранено: конфиг недоступен для записи — используется "
                "пакетный шаблон (настройте GC_HOME или запустите "
                "gc-ua --init-config)",
                outcome="warn",
            )
            return
        try:
            self._voice_setup.wire_cloud(
                self._config_path, url, token, model, language
            )
        except (ValueError, OSError) as exc:
            self._flash(f"не сохранено: {exc}", outcome="warn")
            return
        reloaded = self._voice_hot_reload()
        # Вердикт probe НЕ сбрасываем: он про те же значения, что только что сохранены
        # (в отличие от ухода из секции — там значения могли смениться, см. _reset_voice_probe).
        # А вот ЛЕТЯЩАЯ проверка лечь вердиктом уже после сохранения не должна — бампаем
        # generation и гасим «в полёте» (иначе волна крутилась бы вечно).
        self._voice_probe_in_flight = False
        self._voice_probe_gen += 1
        if self._voice_probe_result is not None:
            # Эталон сравнения переносим на то, что покажут пересобранные поля (токен в
            # них не префиллится) — иначе вердикт погас бы сразу, «из-за» самого рендера.
            self._voice_probe_values = (url, "", model, language)
        # Сохранённое — уже не черновик: поля поднимутся из файла, а не из него.
        self._voice_cloud_draft = None
        self._close_or_back()
        if reloaded:
            self._flash("свой endpoint подключён")
        else:
            self._flash(f"сохранено, {_MSG_APPLIES_NEXT_START}")

    # --- секция «Режимы работы» (дизайн 2026-07-18-settings-work-modes-design §3-4) ---
    def _modes_is_child(self, name: str) -> bool:
        """Ребёнок пресета (`spec.dotted` ∈ чьего-то `preset_keys`) — родовой
        сигнал ревизии 7 (флэт: иерархия только цветом, без маркера/отступа —
        `_modes_tree_glyph` упразднён). Мирроринг
        `command_palette.py::_owning_preset` (тот же приём, без общего
        модуля — см. `_modes_columns`/`command_palette.py::_toggle_columns`)."""
        from gc_tui.toggles import TOGGLES

        spec = TOGGLES[name]
        if spec.preset_keys:
            return False
        return any(
            spec.dotted in pspec.preset_keys
            for pspec in TOGGLES.values()
            if pspec.preset_keys
        )

    def _modes_head(self, name: str) -> str:
        """Голова строки секции — `имя команды · подпись`, одинаково для ВСЕХ строк.

        Прежде голое `spec.label` полагалось «прочим» (не пресет и не чей-то
        ребёнок), но таких в секции не было ни одного, а первый же — `level` —
        показал, что правило неверное: соседи все с именем команды впереди, и
        строка без него выпадала бы из ряда.

        Подпись берётся из `settings_label`, если тоггл её объявил: секция правит
        ФАЙЛ, палитра тем же тогглом крутит сессию, и «по умолчанию» верно только
        здесь."""
        from gc_tui.toggles import TOGGLES

        spec = TOGGLES[name]
        return f"{name} · {spec.settings_label or spec.label}"

    def _modes_columns(self) -> tuple[int, int, int, int]:
        """(label_w, base_w, extra_w, value_w) — общие колонки строк секции
        (выравнивание ревизия 5, спека §4.2/§6: тот же контракт и тот же
        split базы/резерва, что у палитры — `command_palette.py::_toggle_columns`).
        label_w — макс. ширина головы строки: «name · label» и у пресета, и у
        ребёнка (ревизия 7 — flat, маркера/отступа нет, оба head в одном
        формате — `_modes_head`, одна формула на все строки) + 3 + 2 на курсор;
        base_w — макс. ширина БАЗОВОГО трека (2*n-1 ячеек по значениям
        состава БАЗЫ — `base_toggle_values`, статика или `values_provider`);
        extra_w — 3 ячейки у preset-тоггла (4-я позиция custom
        зарезервирована в Settings всегда, спека §4.2), 0 у single-key
        тогглов; value_w (ревизия 6 п.3) — фикс-ширина колонки подписи
        значения: макс. длина имени значения по ВСЕМ тогглам секции,
        ПЛЮС `PRESET_CUSTOM` — пресет-строка может показать «custom», хотя
        оно не входит в `spec.names` ни одного тоггла. Точный контракт брал
        бы max только по строкам, где custom реально достижим (preset-
        тогглы); здесь — упрощённо max по ВСЕМ именам+custom сразу (custom
        никогда не оказывается ДЛИННЕЕ реальных имён значений — упрощение не
        сужает колонку ни для одной строки, только потенциально резервирует
        на символ-два больше, чем строго необходимо, зато без разбора
        preset/не-preset)."""
        from gc_tui.toggles import (
            PRESET_CUSTOM,
            TOGGLES,
            base_toggle_values,
            modes_section_names,
        )

        names = modes_section_names()
        widths = [len(self._modes_head(name)) for name in names]
        label_w = max(widths) + 3 + 2
        base_w = 0
        extra_w = 0
        value_names = [PRESET_CUSTOM]
        for name in names:
            spec = TOGGLES[name]
            values = base_toggle_values(self.app, name)
            base_w = max(base_w, 2 * len(values) - 1)
            if spec.preset_keys:
                extra_w = max(extra_w, 3)
            value_names.extend(v.value for v in values)
        value_w = max(len(v) for v in value_names)
        return label_w, base_w, extra_w, value_w

    def _modes_tail_w(self) -> int:
        """Ширина хвоста строки режима: трек, зазор, подпись значения."""
        _label_w, base_w, extra_w, value_w = self._modes_columns()
        return base_w + extra_w + 2 + value_w

    def _mode_row_text(
        self, name: str, selected: bool, width: int = 0
    ) -> Text:
        from gc_tui.toggles import (
            PRESET_CUSTOM,
            TOGGLES,
            base_toggle_values,
            padded_track,
            toggle_state,
            toggle_state_base,
        )

        spec = TOGGLES[name]
        if spec.value_text is not None:
            return self._value_row_text(name, selected, width)
        current = toggle_state_base(
            self.app, name
        )  # секция правит глобал → трек = глобал
        label_w, base_w, extra_w, value_w = self._modes_columns()
        is_child = self._modes_is_child(name)
        head = self._modes_head(name)
        extra = (current == PRESET_CUSTOM) if spec.preset_keys else None
        values = base_toggle_values(self.app, name)
        track = padded_track(
            spec,
            current,
            extra_lit=extra,
            base_w=base_w,
            extra_w=extra_w,
            values=values,
        )
        t = Text()
        t.append(
            "▸ " if selected else "  ", style=ACCENT
        )  # курсор — конвенция меню/«Обновления»
        # Ревизия 7 (спека §4.2): пресет — head целиком bold INK (без слеша);
        # ребёнок — flat, БЕЗ маркера и БЕЗ отступа (тот же левый край, что и
        # родитель) — имя+описание целиком SUB_INK, иерархия только цветом.
        if spec.preset_keys:
            t.append(head.ljust(label_w - 2), style=f"bold {INK}")
        elif is_child:
            t.append(head.ljust(label_w - 2), style=SUB_INK)
        else:
            t.append(head.ljust(label_w - 2), style=INK)
        # Выравнивание ревизия 5 (спека §6): базовые треки — по ЛЕВОМУ краю
        # общего поля (тройки друг под другом), custom-резерв правее них;
        # паддинг у `padded_track` целиком справа — прежний left-pad перед
        # треком (право-прижатое поле) убран.
        t.append_text(track)
        t.append("  ")
        # Ревизия 6 п.3: фикс-ширина колонки подписи значения (`value_w`) —
        # хвост «в текущей сессии» ниже стартует в одной колонке у всех строк
        # секции, независимо от длины САМОГО значения этой строки. Ревизия 7:
        # подпись значения ребёнка — тоже SUB_INK (трек между head и подписью —
        # семантические цвета, не задет), мирроринг
        # `command_palette.py::_format_toggle_row`.
        t.append(current.ljust(value_w), style=SUB_INK if is_child else INK)
        # §6.2: значение, действующее в сессии, если оно не совпадает с
        # файловым, — второй строкой ровно под именем ключа (решение владельца
        # 2026-10-18: хвост в той же строке на узком окне переносился куда
        # попало). Подпись — самым тусклым цветом и курсивом (не спутать с
        # дочерними строками), значение — цветом своей позиции трека.
        effective = toggle_state(self.app, name)
        if effective != current:
            t.append("\n  ")
            t.append("в текущей сессии: ", style=f"italic {DEEP_DIM}")
            colours = {v.value: v.color for v in values}
            t.append(effective, style=colours.get(effective, DIM))
        return fit_row_head(t, label_w, self._modes_tail_w(), width)

    def _value_row_text(
        self, name: str, selected: bool, width: int = 0
    ) -> Text:
        """Строка без трека (`value_text`): голова и знак значения в колонке
        треков; выбор сессии, если он другой, — второй строкой, как у
        соседей (§6.2)."""
        from gc_tui.toggles import TOGGLES

        spec = TOGGLES[name]
        label_w = self._modes_columns()[0]
        t = Text()
        t.append("▸ " if selected else "  ", style=ACCENT)
        t.append(self._modes_head(name).ljust(label_w - 2), style=INK)
        base = spec.value_text(self.app, self.app.base_settings())
        t.append_text(base)
        effective = spec.value_text(self.app, self.app.effective_settings())
        if effective.plain != base.plain:
            t.append("\n  ")
            t.append("в текущей сессии: ", style=f"italic {DEEP_DIM}")
            t.append_text(effective)
        return fit_row_head(t, label_w, self._modes_tail_w(), width)

    def _modes_row_opens_picker(self) -> bool:
        """Строка под курсором выбирается списком (`opens_picker`)."""
        from gc_tui.toggles import TOGGLES, modes_section_names

        names = modes_section_names()
        return bool(names) and TOGGLES[names[self._modes_idx]].opens_picker

    def _modes_value_plain(self, name: str) -> str:
        """Записанное значение строки словами — для превью меню."""
        from gc_tui.toggles import TOGGLES, toggle_state_base

        spec = TOGGLES[name]
        if spec.value_words is not None:
            return spec.value_words(self.app, self.app.base_settings())
        return toggle_state_base(self.app, name)

    def _refresh_modes_rows(self) -> None:
        """Строки режимов под фактическую ширину: не влезает — подпись с «…»,
        строка не переносится (`fit_row_head`). Последние len(names) строк —
        старые ещё могут не отсоединиться (приём `_voice_row_widgets`)."""
        if self._view != _MODES:
            return
        from gc_tui.toggles import modes_section_names

        names = modes_section_names()
        rows = list(self.query(".set-modes-row"))[-len(names) :]
        if len(rows) != len(names):
            return
        for i, (name, row) in enumerate(zip(names, rows)):
            row.update(
                self._mode_row_text(name, i == self._modes_idx, row.size.width)
            )

    def _mount_modes_section(self, frame: Vertical) -> None:
        from gc_tui.toggles import TOGGLES, modes_section_names

        names = modes_section_names()
        self._modes_idx = min(self._modes_idx, len(names) - 1)
        for i, name in enumerate(names):
            selected = i == self._modes_idx
            # Разделитель групп (ревизия 7, спека §4.2): родовой по
            # `preset_keys`, ОБЩИЙ приём с палитрой
            # (`command_palette.py::_group_sep_count`/`_build_footer`) — черта
            # перед КАЖДЫМ preset-родителем, который не первая строка секции.
            # Сегодня в TOGGLES одна preset-группа (`rigor`) → по этому признаку
            # не рисуется. Второй признак — СМЕНА `group`: секция собрана из
            # нескольких групп (`MODES_SECTION_GROUPS`), и `level` стоит своим
            # блоком под чертой. Признака два, потому что вопросы разные: один
            # отделяет пульт пресета, другой — блок секции; ни один не выражается
            # через другой.
            if i > 0 and (
                TOGGLES[name].preset_keys
                or TOGGLES[name].group != TOGGLES[names[i - 1]].group
            ):
                frame.mount(Rule(classes="set-group-sep"))
            # Без set-spaced: плотность B — тогглы группы впритык (общий пульт),
            # воздух даёт разделитель групп (margin-top .set-group-sep).
            cls = "set-row set-dyn set-modes-row" + (
                _CLS_SELECTED if selected else ""
            )
            frame.mount(
                Static(self._mode_row_text(name, selected), classes=cls)
            )
        # Ширина строки известна только после раскладки: подпись под неё
        # подрезается вторым проходом (тот же приём, что `_refresh_menu_rows`).
        self.call_after_refresh(self._refresh_modes_rows)
        # Подсказка выделенного тоггла — ВНУТРИ рамки (конвенция «Обновления»:
        # пояснительная строка в карточке под строками-опциями, класс set-hint);
        # высота — под самый длинный вариант секции (дизайн-код §9).
        frame.mount(
            ReservedHint(
                self._modes_hint_block(),
                variants=self._modes_hint_variants,
                classes="set-hint set-dyn set-modes-hint",
            )
        )

    def _modes_hint_text(self) -> str:
        """Подсказка выделенной строки целиком (без клавиш — их держит
        футер): значение + подсказка ГЛОБАЛЬНОГО состояния."""
        from gc_tui.toggles import modes_section_names, toggle_state_base

        names = modes_section_names()
        if not names:
            return ""
        name = names[self._modes_idx]
        return self._modes_value_hint(name, toggle_state_base(self.app, name))

    def _modes_value_hint(self, name: str, value: str) -> str:
        """Подсказка строки ``name`` при значении ``value``. Строка-пресет в
        custom — динамический custom-хинт (спека §5); строка со списком —
        подсказка модели."""
        from gc_tui.toggles import (
            PRESET_CUSTOM,
            TOGGLES,
            dynamic_toggle_hint,
            preset_custom_hint,
        )

        spec = TOGGLES[name]
        if spec.opens_picker:
            return self._model_row_hint()
        # Динамический хинт вперёд статического — тот же порядок, что у палитры
        # (`CommandPalette(toggle_dynamic_hint=...)`): у `level` статических
        # подсказок нет вовсе, их роль играет модель, резолвнутая для уровня.
        if spec.hint_provider is not None:
            hint = dynamic_toggle_hint(self.app, name, value)
        elif value in spec.names:
            hint = spec.values[spec.names.index(value)].hint
        elif spec.preset_keys and value == PRESET_CUSTOM:
            hint = preset_custom_hint(self.app, name, scope="global")
        else:
            value, hint = spec.values[0].value, spec.values[0].hint
        return f"{value} — {hint}"

    @staticmethod
    def _modes_key_text(name: str) -> str:
        """Строка ключа конфига строки ``name`` (пусто — ключа нет)."""
        from gc_tui.toggles import TOGGLES

        dotted = TOGGLES[name].dotted
        return f"ключ: {dotted}" if dotted else ""

    def _modes_hint_block(self) -> Text:
        """Подсказка внизу рамки: значение выделенной строки, второй строкой
        — её ключ конфига (`ключ: <dotted>`, имя заметно ярче — `SUB_INK`).
        Место ключа есть всегда (у строки без ключа — пустое)."""
        from gc_tui.toggles import modes_section_names

        t = Text(self._modes_hint_text() + "\n")
        names = modes_section_names()
        key = self._modes_key_text(names[self._modes_idx]) if names else ""
        if key:
            label, _, dotted = key.partition(" ")
            t.append(label + " ").append(dotted, style=SUB_INK)
        return t

    def _modes_hint_variants(self) -> list[str]:
        """Все подсказки секции — каждая строка при каждом своём значении,
        с её строкой ключа: резерв высоты. Подсказки, что собираются на
        ходу (модель, custom пресета), — по текущему значению."""
        from gc_tui.toggles import (
            TOGGLES,
            modes_section_names,
            toggle_state_base,
        )

        out = []
        for name in modes_section_names():
            spec = TOGGLES[name]
            values = list(spec.names)
            current = toggle_state_base(self.app, name)
            if current not in values:
                values.append(current)
            key = self._modes_key_text(name)
            out.extend(
                f"{self._modes_value_hint(name, v)}\n{key}" for v in values
            )
        return out

    def _file_snapshot(self, target):
        """Текст файла до записи — или None, если его ещё нет либо он не читается
        (тогда откатывать нечего: не было файла, не будет и следа).

        Зовётся ДО `try` вызывающего — исключение отсюда уходит мимо всех его
        перехватов и роняет приложение целиком."""
        from gc_core.utils.user_text import read_user_text_or_none

        return read_user_text_or_none(target)

    def _restore_snapshot(self, target, snapshot) -> bool:
        """Вернуть файл к снимку. Возвращает True, если откат не требовался или
        удался, False — не удалось: тогда notice вызывающего обязан сказать об
        этом отдельной строкой, а не промолчать поверх первого отказа (ревью
        MR #80, L32).

        Запись — атомарным писателем модуля (`onboarding_config_writer.write_text`),
        как и любая другая запись конфига в этом экране: снимок и так лежал на
        диске, второй валидации ему не нужно. Прежний прямой `Path.write_text`
        усекал цель до нуля и только потом наполнял — прерывание между этими двумя
        шагами оставляло config.yml пустым."""
        if snapshot is None:
            return True
        from gc_core.utils.user_text import read_user_text_or_none

        # Нечитаемый текущий файл — не повод бросить откат: снимок писался ПЕРЕД
        # записью, значит вернуть его надо тем более. Сравнение экономит запись,
        # но не отменяет её.
        if read_user_text_or_none(target) == snapshot:
            return True
        from gc_core import onboarding_config_writer as ocw

        try:
            ocw.write_text(target, snapshot)
        except OSError:
            return False
        return True

    def _overriding_layer_name(self, values: dict) -> str:
        """Как назвать слой, перекрывший записанное значение.

        Перечислением («проектный конфиг или профиль») эту фразу писать нельзя: она
        врёт в первой половине. Проектный конфиг, который задаёт ключ, перехватывает
        саму ЗАПИСЬ (`_keys_write_target`) и перекрыть её уже не может; остаются
        файл профиля, куда секция не пишет вовсе, и machine-managed оверлей
        `~/.gc/config.local.yml`, который правит `gc-mcp`, а не человек.

        Слой называется поимённо тем же `config_origin`, которым секция объясняет
        перекрытие полей визарда и адреса голоса, — второго механизма для того же
        вопроса не заводим. Не смогли определить — говорим общо: сам факт «действует
        другое» важнее имени файла и уже сказан.

        Указатель профиля здесь спрашивается У ФАЙЛОВ (`profile_override` НЕ
        передаётся) — в отличие от соседних витрин происхождения, и это не недосмотр
        свипа: они отвечают «что действует сейчас», а эта — о судьбе ЗАПИСАННОГО
        ключа. Под `gc-ua --profile ""` профиль из файла в этом запуске не действует,
        но записанное значение он перекроет на любом другом старте — и managed-файл,
        честный ответ «сейчас», послал бы чинить не тот файл. Тем же файловым
        указателем судит гейт перед записью (`_stacked_app_config` собирает стек
        СЛЕДУЮЩЕГО старта): сообщение о судьбе записи и гейт, её пропустивший, не
        имеют права отвечать по-разному об одном ключе."""
        general = "слоем выше"
        if not self._config_path:
            return general
        try:
            from gc_core.config_origin import origins, overlay_label

            found = origins(self._config_path, list(values))
        except Exception:
            return general  # диагностика не обязана ронять запись
        # Ключ выбираем по РАСХОЖДЕНИЮ с записанным, а не первый перекрытый: у пары
        # пресета перекрытыми бывают оба, разными слоями, и первый попавшийся —
        # обычно тот самый проектный файл, который запись как раз перехватил и
        # перекрыть уже не мог. Человека послали бы не туда.
        for dotted, origin in found.items():
            if origin.is_overridden and origin.effective != values.get(dotted):
                return overlay_label(origin)
        for origin in found.values():
            if origin.is_overridden:
                return overlay_label(origin)
        return general

    def _keys_write_target(self, values: dict):
        """Куда пишется набор ключей секции: файл, который их УЖЕ задаёт (проектный
        `.gc/config.yml`), иначе базовый конфиг запуска.

        Правило именно «где ключ задан», а не «есть ли проектный файл»: он
        приезжает с клоном репозитория, и класть туда личную настройку значило бы
        править файл проекта за спиной команды. А писать в базовый ключ, который
        проект перекрывает, — молчаливый no-op: ручка на глазах «не срабатывает».

        Пара пресета (`/rigor` пишет оба своих ключа) уходит ЦЕЛИКОМ туда, где
        задан хотя бы один из них: пресет ставит значения согласованно, и разнести
        их по разным файлам значило бы собрать состояние, которого не выбирали.

        Путь — АБСОЛЮТНЫЙ: он идёт в плашку результата, а проектная цель резолвится
        сама, тогда как базовая приходит ровно такой, какой её передали в `--config`.
        Две соседние плашки одной секции отвечают на один вопрос «куда легло» —
        значит и называть файл обязаны одинаково (живой смоук владельца)."""
        if any(self._project_config_sets(key) for key in values):
            return self._project_config_candidate_path()
        return self._profiles_base_path().resolve()

    def _write_section_keys(self, values: dict):
        """Мгновенная запись ключей секции в config.yml «всё или ничего»:
        цель по `_keys_write_target`, гейт пакетного шаблона, перечитка через
        `_on_apply` и перерисовка. Возвращает путь записи или None — отказ уже
        сказан репликой."""
        from gc_core import onboarding_config_writer as w
        from gc_core.config import _default_config_path, validate_config_text

        target = self._keys_write_target(values)
        # Гейт пакетного шаблона — ОДИН и по ЦЕЛИ, как у соседних записей секции
        # «Профили». Прежняя предпроверка по пути запуска стояла раньше него и
        # спорила с правилом выбора файла: проектный `.gc/config.yml` писуч всегда,
        # и запрещать запись туда из-за пакетной базы было не за что.
        if not self._target_writable(target):
            self._notice(self._TEMPLATE_WRITE_REFUSAL)
            return None
        anchors = [key for key in values if "." not in key]
        # Снимок ДО якорей: `ensure_pointer_anchor` — отдельная запись на диск и
        # валидации не знает, а `write_values` ниже отвергает итог целиком. Без
        # отката отказ оставлял бы дописанный `level:` со ЗНАЧЕНИЕМ ИЗ ШАБЛОНА:
        # действующий уровень молча менялся, а человеку говорили «не сохранено».
        # «Всё или ничего» — обещание всей секции, а не одной её записи.
        snapshot = self._file_snapshot(target) if anchors else None
        try:
            for key in anchors:
                # Топ-уровневый скаляр (`level:`) `write_values` сам дописать не
                # умеет — его фоллбэк на шаблон работает только для dotted-ключей,
                # а конфиг, созданный до появления ключа, этой строки не содержит.
                w.ensure_pointer_anchor(
                    target, template_path=_default_config_path(), key=key
                )
            missing = w.write_values(
                target,
                values,
                template_path=_default_config_path(),
                validate=validate_config_text,
            )
        except (ValueError, OSError) as exc:
            restored = self._restore_snapshot(target, snapshot)
            message = f"✗ не сохранено: {exc}"
            if not restored:
                message += f"; конфиг остался изменённым: {target}"
            self._notice(message)
            return None
        if missing:
            # Файл не тронут (запись «всё или ничего») — молчать нельзя, иначе слайдер
            # покажет новое значение, которого в конфиге нет (находка ревью коллеги).
            restored = self._restore_snapshot(target, snapshot)
            message = f"✗ не сохранено: не нашлись ключи {', '.join(missing)}"
            if not restored:
                message += f"; конфиг остался изменённым: {target}"
            self._notice(message)
            return None
        # Хвост записи — как у соседних секций (_do_save): перечитать сырые значения
        # файла и применить через инъектированный _on_apply, а не звать reload мимо него.
        self._raw = self._read_raw()
        try:
            self._on_apply()
            self._pull_base_from_app()
        except Exception as exc:
            self._notice(f"сохранено, но конфиг не перечитан: {exc}")
            return None
        self._render_view()
        return target

    def _modes_apply(self, direction: int) -> None:
        """←/→ на строке тоггла (канон §1/§3): мгновенно записать
        переключённое ГЛОБАЛЬНОЕ значение в config.yml (Settings = глобальный
        дефолт, дизайн 2026-07-21 §3). Цикл значения — от ТЕКУЩЕГО глобального
        состояния, по составу базы (`base_toggle_names` — статика или
        `values_provider`, без разбора по имени тоггла). Строка, значение
        которой выбирается списком (`opens_picker`), шагом не меняется. Guard
        записи — как у остальных секций (_writable): пакетный шаблон не пишем.
        Ошибка — флеш, значение не меняется. Сессионные переопределения НЕ
        трогаем — их перекрытие показывает пометка §6.2."""
        from gc_tui.toggles import (
            PRESETS,
            TOGGLES,
            base_toggle_names,
            cycle_in,
            modes_section_names,
            toggle_state_base,
        )

        names = modes_section_names()
        name = names[self._modes_idx]
        spec = TOGGLES[name]
        if spec.opens_picker:
            return  # значение выбирается списком, не шагом
        current = toggle_state_base(self.app, name)
        cycle_names = base_toggle_names(self.app, name)
        new_value = cycle_in(cycle_names, current, direction)
        values = (
            PRESETS[new_value]
            if spec.preset_keys
            else {spec.dotted: new_value}
        )
        target = self._write_section_keys(values)
        if target is None:
            return
        # Записанное значение может быть перекрыто слоем выше (файл профиля —
        # туда секция не пишет вовсе, и выбором целевого файла случай не
        # закрывается). Честнее сказать, что действует, чем оставить трек стоять
        # на выбранном значении, которого нет.
        effective = toggle_state_base(self.app, name)
        if effective != new_value:
            self._notice(
                f"записано в {_shown_path(target)}, "
                f"но действует «{effective}» — "
                f"значение перекрыто {self._overriding_layer_name(values)}"
            )
            return
        # Плашка называет ФАЙЛ: запись уходит не всегда в один и тот же конфиг
        # (проектный перехватывает ключ, который сам задаёт), и «сохранено» без
        # имени оставляло бы человека гадать, где искать результат.
        self._flash(
            f"«{self._modes_head(name)}» — {new_value}, "
            f"записано в {_shown_path(target)}"
        )

    # --- «Режимы работы»: модель по умолчанию (спека model-knob §6.6) ---
    _MODEL_TARGET_TEXTS = {
        "profile": "запишется в профиль {name} ({path})",
        "project": "запишется в проектный конфиг ({path})",
        "base": "запишется в основной конфиг ({path})",
    }
    _MODEL_PROFILE_MISSING = "файл профиля {name} не найден"

    def _model_write_target(
        self,
    ) -> tuple[Optional[Path], Literal["profile", "project", "base"]]:
        """(путь, вид) записи `model`: активный профиль — его файл, где бы
        он ни лежал (решение владельца); иначе общее правило секции
        (`_keys_write_target`): проектный конфиг, если ключ задан там, или
        основной. Файла активного профиля нет — путь None: в основной
        конфиг молча не уходим, запись откажет."""
        name = self._config_profile()
        if name:
            row = self._profile_row_by_name(name)
            return (row["ref"].path if row else None), "profile"
        target = self._keys_write_target({"model": None})
        kind = "project" if self._project_config_sets("model") else "base"
        return target, kind

    def _model_row_hint(self) -> str:
        """Подсказка строки: про записанную модель (как под списком),
        клавиша списка и файл, куда ляжет выбор."""
        from gc_tui.services.model_picker import model_hint, model_name

        value = model_name(self.app, self._settings)
        path, kind = self._model_write_target()
        name = self._config_profile()
        target = (
            self._MODEL_TARGET_TEXTS[kind].format(
                name=name, path=_shown_path(path)
            )
            if path is not None
            else self._MODEL_PROFILE_MISSING.format(name=name)
        )
        about = model_hint(self.app, value, self._settings)
        return f"{about} · Enter — выбрать из списка · {target}"

    def _open_model_picker(self) -> None:
        """Enter на строке модели: список моделей над подсказкой раздела.
        Состав и «активна» — по настройкам цели записи (база окна), не
        по сессии: секция правит файл."""
        from gc_tui.services.model_picker import (
            model_hint,
            model_name,
            model_picker_options,
        )
        from gc_tui.widgets.arg_picker import ArgPicker

        hints = list(self.query(_SEL_MODES_HINT))
        if self._modes_picker is not None or not hints:
            return
        # Клавиши списка называет футер экрана: строка клавиш пикера
        # обещала бы Tab, которого здесь нет. Открывается на записанном
        # значении — Enter сразу не перепишет файл чужим выбором.
        picker = ArgPicker(
            model_picker_options(self.app, settings=self._settings),
            hint_provider=lambda value: model_hint(
                self.app, value, self._settings
            ),
            initial_value=model_name(self.app, self._settings),
            keys_hint=None,
        )
        self._cap_model_picker(picker)
        hints[-1].parent.mount(picker, before=hints[-1])
        self._modes_picker = picker
        self._refresh_footer()

    def _cap_model_picker(self, picker) -> None:
        """Потолок списка — 80% области прокрутки экрана. `max-height: 80%`
        у `.palette` здесь не действует: рамка секции — `height: auto`, и
        процент Textual не от чего считать; без потолка длинный реестр
        вытягивал список на всю свою длину."""
        limit = self.query_one(_SEL_SET_SCROLL).styles.max_height
        if limit is not None and limit.is_cells:
            picker.styles.max_height = max(1, int(limit.value * 0.8))

    def _close_model_picker(self) -> None:
        if self._modes_picker is None:
            return
        self._modes_picker.remove()
        self._modes_picker = None
        self._refresh_footer()

    def _model_picker_key(self, key: str) -> None:
        """Список держит клавиатуру: ↑/↓ — выбор, Enter — запись
        (недоступная строка не берётся), прочее молчит."""
        picker = self._modes_picker
        if key in ("up", "down"):
            picker.move_selection(-1 if key == "up" else 1)
            return
        if key != "enter" or picker.selected_disabled():
            return
        value = picker.selected_value()
        self._close_model_picker()
        if value:
            self._write_model_default(value)

    def _write_model_default(self, choice: str) -> Optional[Path]:
        """Записать модель по умолчанию в цель (`_model_write_target`) и
        сказать итог: плашка — значение и файл; перекрыто — что действует
        и чем перекрыто. Возвращает путь записи или None (отказ сказан)."""
        path, kind = self._model_write_target()
        if path is None:
            name = self._config_profile()
            self._notice(
                "✗ не сохранено: "
                + self._MODEL_PROFILE_MISSING.format(name=name)
            )
            return None
        if kind == "profile":
            target = self._write_profile_model(path, choice)
        else:
            target = self._write_section_keys({"model": choice})
        if target is None:
            return None
        warning = self._model_override_warning(choice, target)
        if warning is not None:
            self._notice(warning)
        else:
            self._flash(
                f"«Модель по умолчанию» — {choice}, "
                f"записано в {_shown_path(target)}"
            )
        return target

    def _write_profile_model(self, path, choice: str):
        """`model:` в файл профиля — писателем секций моделей с проверкой
        документа профиля (стек следующего старта, как у записей реестра в
        профиль)."""
        from gc_core.config_modules.registry_write import (
            RegistryWriteError,
            apply_registry_ops,
        )

        row = self._profile_row_by_name(self._config_profile())
        block = self._profile_write_block(ref=row["ref"])
        if block:
            self._notice(self._profile_write_refusal("сохранено", block))
            return None
        try:
            apply_registry_ops(
                path,
                [("write_pointer", "model", choice)],
                "",
                is_profile=True,
                stack_base=self._config_path,
            )
        except (RegistryWriteError, OSError) as exc:
            self._notice(f"✗ не сохранено: {exc}")
            return None
        if not self._reload_after_profile_file_change("сохранено"):
            return None
        self._render_view()
        return path

    def _model_override_warning(self, choice: str, target):
        """Почему записанное не действует, или None. Перекрыть его может
        слой выше (`_overriding_layer_name`) и сессия: выбор `/model` или
        профиль сессии."""
        from gc_tui.toggles import toggle_state, toggle_state_base

        acting = toggle_state_base(self.app, "model")
        if acting != choice:
            layer = self._overriding_layer_name({"model": choice})
        else:
            acting = toggle_state(self.app, "model")
            if acting == choice:
                return None
            layer = self._session_layer_name()
        return (
            f"записано в {_shown_path(target)}, но действует «{acting}» — "
            f"значение перекрыто {layer}"
        )

    def _session_layer_name(self) -> str:
        """Что в сессии перекрыло записанную модель."""
        overrides = getattr(self.app, "_session_overrides", None) or {}
        profile = self._session_profile_name()
        if "model" not in overrides and profile:
            return f"профилем сессии {profile}"
        return "выбором сессии /model"

    # --- секция «Внешний вид»: тема `tui.theme` ---
    def _theme_value(self) -> str:
        from gc_core.settings import settings_of

        return settings_of(self._settings).tui.theme

    def _theme_name(self) -> str:
        return THEME_NAMES[self._theme_value()]

    def _mount_look_section(self, frame: Vertical) -> None:
        """Одна строка «Тема» с переключателем `◂ имя ▸`: без трека, имя и
        есть вся шкала, а стрелки (тусклее имени) говорят, что значение
        листается ←/→. Строка в секции одна и всегда выделена, поэтому
        стрелки видны всегда. Под ней — пояснение."""
        label_w = len(_THEME_LABEL) + 3
        t = Text()
        t.append("▸ ", style=ACCENT)
        t.append(_THEME_LABEL.ljust(label_w), style=INK)
        t.append("◂ ", style=SUB_INK)
        t.append(self._theme_name(), style=INK)
        t.append(" ▸", style=SUB_INK)
        frame.mount(Static(t, classes="set-row set-dyn" + _CLS_SELECTED))
        frame.mount(
            ReservedHint(
                _THEME_HINTS[self._theme_value()],
                variants=_THEME_HINTS.values,
                classes="set-hint set-dyn set-theme-hint",
            )
        )

    def _look_apply(self, direction: int) -> None:
        """←/→ — следующая тема по кругу, мгновенная запись `tui.theme` тем же
        путём, что у «Режимов работы». Палитра выбирается до импорта
        приложения, поэтому плашка говорит, что тема сменится при следующем
        запуске."""
        from gc_tui.toggles import cycle_in

        new_value = cycle_in(list(THEME_NAMES), self._theme_value(), direction)
        values = {"tui.theme": new_value}
        target = self._write_section_keys(values)
        if target is None:
            return
        if self._theme_value() != new_value:
            self._notice(
                f"записано в {_shown_path(target)}, но действует "
                f"«{self._theme_name()}» — значение перекрыто "
                f"{self._overriding_layer_name(values)}"
            )
            return
        self._flash(
            f"«{_THEME_LABEL}» — {THEME_NAMES[new_value]}, записано в "
            f"{_shown_path(target)}; {_MSG_APPLIES_NEXT_START}"
        )

    # --- секция «Профили» (витрина обоих корней + единое правило записи) ---
    def _profiles_base_path(self):
        from pathlib import Path

        from gc_core.config import _default_config_path

        return Path(self._config_path or _default_config_path())

    def _profiles_rows(self) -> list[dict]:
        """Строки секции: одна на каждое найденное имя профиля (тёзки — ОДНОЙ
        строкой, побеждает проектный) + «+ добавить профиль…» последней. Уровень
        сюда больше не входит — он правит глобальный ключ конфига и живёт в
        «Режимах работы»."""
        import os
        from pathlib import Path

        from gc_core.config_modules.profiles import (
            discover_all_refs,
            profile_roots,
        )

        roots = profile_roots(Path(os.getcwd()), self._profiles_base_path())
        grouped = discover_all_refs(roots)
        rows = []
        for name in sorted(grouped):
            refs = grouped[name]
            rows.append(
                {
                    "kind": "profile",
                    "name": name,
                    "ref": refs[0],
                    "shadow": refs[1] if len(refs) > 1 else None,
                }
            )
        if rows:
            # «— без профиля» — первой строкой: глобальная деактивация; в пустом
            # состоянии (профилей нет) строка бессмысленна — деактивировать нечего.
            rows.insert(0, {"kind": "none"})
        # «+ добавить профиль» — последней строкой списка (перенос паттерна
        # «Моделей», ревизия владельца): Enter = клавиша n. Есть и в пустом
        # состоянии — там это единственная строка-действие.
        rows.append({"kind": "add"})
        return rows

    def _profiles_active_index(self) -> int:
        """Строка с отметкой «активен» — стартовый курсор секции: профиль
        базы экрана, без профиля — «без профиля» (первая строка)."""
        active = self._config_profile()
        rows = self._profiles_rows()
        return next(
            (i for i, r in enumerate(rows) if r.get("name") == active), 0
        )

    def _profiles_on_add_row(self) -> bool:
        """Курсор — на строке «+ добавить профиль…» (последняя): у неё свой
        футер, клавиши объектов на ней не работают."""
        rows = self._profiles_rows()
        return (
            0 <= self._profiles_idx < len(rows)
            and rows[self._profiles_idx]["kind"] == "add"
        )

    def _profiles_on_none_row(self) -> bool:
        """Курсор — на строке «— без профиля». У неё свой глагол (снять выбор) и
        своё пустое состояние (снимать нечего), поэтому футер тоже свой."""
        rows = self._profiles_rows()
        if not rows:
            return False
        idx = min(self._profiles_idx, len(rows) - 1)
        return rows[idx]["kind"] == "none"

    def _project_config_candidate_path(self):
        from gc_core.config_modules.env_vars import project_gc_dir

        return project_gc_dir() / "config.yml"

    def _profile_summary(self, path):
        """Сводка файла профиля через кэш прохода (`_profiles_disk_cache`).

        Чтение + разбор YAML — самая дорогая часть отрисовки секции: строки
        спрашивают минимум четыре независимых места за кадр (футер, монтирование,
        refresh строк, refresh подсказки), и без кэша каждый файл парсился заново
        на каждом из них. Кэш живёт один проход UI — сбрасывается на входе в
        `_render_view` и на обработке клавиши, поэтому правка профиля извне
        по-прежнему видна с первого же нажатия."""
        from gc_core.config_modules.profiles import profile_display_summary

        cache = self._profiles_disk_cache.setdefault("summaries", {})
        key = str(path)
        if key not in cache:
            cache[key] = profile_display_summary(path)
        return cache[key]

    def _project_config_doc(self) -> dict:
        """Сырой документ проектного `.gc/config.yml` через тот же кэш прохода:
        его спрашивает КАЖДАЯ строка списка (источник записи, пометка «выбран
        проектом»), то есть N раз за проход вместо одного."""
        cache = self._profiles_disk_cache
        if "project_doc" not in cache:
            from gc_core import config_raw
            from gc_core.config_modules.env_vars import (
                project_local_config_path,
            )

            project_path = project_local_config_path()
            cache["project_doc"] = (
                config_raw.read_document(project_path)
                if project_path is not None
                else {}
            )
        return cache["project_doc"]

    def _project_config_sets(self, key: str) -> bool:
        """Проектный `.gc/config.yml` уже несёт ключ `key` (любое значение, даже
        пустую строку — deep-merge отдаёт победу оверлею по факту наличия ключа, не
        по его «истинности») — тогда запись ЭТОГО ключа в базовый конфиг была бы
        молчаливым no-op: проектный слой всё равно перекроет её.

        Ключ — ПОЛНЫЙ путь (`tui.finish_gate_mode`), не только топ-уровневый:
        `deep_merge` рекурсивен, и наличие одного лишь `tui:` в проекте не
        перекрывает ничего — перекрыт ровно тот лист, который проект назвал.
        Судить по верхнему ключу значило бы уводить в проектный файл записи,
        которые прекрасно легли бы в базовый."""
        node = self._project_config_doc()
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        return True

    def _project_sets_profile_pointer(self) -> bool:
        return self._project_config_sets("profile")

    def _profile_activation_target(self, ref):
        """Единое правило записи: проектный профиль → проектный конфиг (создаётся
        при отсутствии) — указатель на него никогда не уходит в базовый. Всё
        остальное — тем же общим правилом, что и снятие (`_deactivation_target`):
        слой, где запись возымеет эффект. Спрашиваем его, а не повторяем условие
        своими словами: своя редакция уже разъезжалась бы с чужой на первой правке,
        а цена расхождения — молчаливый no-op (проектный слой перекроет запись)."""
        if ref.root_kind == "project":
            return self._project_config_candidate_path()
        return self._deactivation_target()

    def _write_profile_pointer(
        self, path, name: str, *, extra_candidates=None
    ) -> None:
        """Запись `profile: "<name>"` — общей склейкой примитивов указателя
        (`onboarding_config_writer.write_profile_pointer`, там же и все доводы):
        её второй потребитель — финал мастера на сломанном профиле, и своя
        редакция здесь разъехалась бы с ним на первой же правке."""
        from gc_core import onboarding_config_writer as ocw

        ocw.write_profile_pointer(
            path,
            name,
            base_path=self._profiles_base_path(),
            extra_candidates=extra_candidates,
        )

    def _deactivation_target(self):
        """Куда пишется `profile: ""` при деактивации — общее правило
        (`config_modules.layers.profile_pointer_write_target`, там же доводы), то
        же самое, что у активации: слой, где запись возымеет эффект. Проектный
        документ отдаём из кэша прохода — файл уже прочитан этим кадром."""
        from gc_core.config_modules.layers import profile_pointer_write_target

        return profile_pointer_write_target(
            self._profiles_base_path(), project_doc=self._project_config_doc()
        )

    def _file_profile_pointer(self):
        """Указатель профиля В ФАЙЛАХ (база, managed, проект) — без флага
        запуска `--profile`: именно его пишут активация и деактивация. Под
        `gc-ua --profile X` `_config_profile()` вернул бы X, и экран отвечал бы
        «уже активен» / писал `profile: ""` мимо того, что в файле."""
        if getattr(self.app, "_profile_override", None) is None:
            return self._config_profile()
        from gc_core.config_modules.layers import resolve_profile_pointer

        try:
            return resolve_profile_pointer(self._profiles_base_path()) or None
        except (OSError, ValueError):
            return self._config_profile()

    def _deactivate_profile(self) -> None:
        """Space на «— без профиля» (клавиша перевешена решением В1 волны B):
        деактивация записью `profile: ""` по единому правилу записи; «ничего не
        активно» — честный no-op."""
        config_active = self._file_profile_pointer()
        if config_active is None:
            self._title_note("профиль не выбран")
            return
        if self._refuse_profile_switch():
            return
        target = self._deactivation_target()
        if not self._target_writable(target):
            self._notice(self._TEMPLATE_WRITE_REFUSAL)
            return
        try:
            self._write_profile_pointer(target, "")
        except ValueError as exc:
            self._notice(f"✗ {self._profile_stack_write_refusal('снят', exc)}")
            return
        except OSError as exc:
            self._notice(f"✗ не сохранено: {exc}")
            return
        try:
            self._on_apply()
            self._pull_base_from_app()
        except Exception as exc:
            self._notice(
                f"записано в {_shown_path(target)}, "
                f"но конфиг не перечитан: {exc}"
            )
            return
        self._raw = self._read_raw()
        self._origins = self._read_origins()
        session_overrides = getattr(self.app, "_session_overrides", None) or {}
        session_name = session_overrides.get("profile")
        if session_name:
            effect = f'текущая сессия остаётся на "{session_name}" (сессионный выбор /profile)'
        else:
            effect = self._launch_profile_effect("") or _MSG_APPLIES_NEXT_TURN
        self._render_view()
        self._flash(f"Профиль деактивирован — {_shown_path(target)}. {effect}")

    def _refuse_profile_switch(self, *, rerender: bool = False) -> bool:
        """Во время хода профиль не меняется: отказ той же фразой, что у
        `/profile`. Им же заперты удаление и переименование профиля и сброс
        конфига — все они переписывают указатель `profile:`. `rerender` —
        отказ на подтверждении: баннер снимается перерисовкой, реплика ложится
        после неё. True — отказано."""
        refusal = profile_switch_refusal(self.app)
        if refusal is None:
            return False
        if rerender:
            self._render_view()
        self._notice(f"✗ {refusal}")
        return True

    def _activate_profile(self, name: str, ref) -> None:
        """Space на строке профиля (клавиша перевешена решением В1 волны B). «Уже
        активен» — сравнение с КОНФИГ-указателем (`_config_profile()`, то, что
        реально пишет активация), не с сессионным выбором: сессия — отдельный
        механизм (`/profile`), запись файла её не трогает."""
        config_active = self._file_profile_pointer()
        if name == config_active:
            self._title_note("уже активен")
            return
        if self._refuse_profile_switch():
            return
        target = self._profile_activation_target(ref)
        if not self._target_writable(target):
            self._notice(self._TEMPLATE_WRITE_REFUSAL)
            return
        try:
            self._write_profile_pointer(target, name)
        except ValueError as exc:
            self._notice(
                "✗ "
                + self._profile_stack_write_refusal(
                    "включён", exc, shown_path=ref.path
                )
            )
            return
        except OSError as exc:
            self._notice(f"✗ не сохранено: {exc}")
            return
        try:
            self._on_apply()
            self._pull_base_from_app()
        except Exception as exc:
            self._notice(
                f"записано в {_shown_path(target)}, "
                f"но конфиг не перечитан: {exc}"
            )
            return
        self._raw = self._read_raw()
        self._origins = self._read_origins()
        session_overrides = getattr(self.app, "_session_overrides", None) or {}
        session_name = session_overrides.get("profile")
        if session_name and session_name != name:
            effect = f'текущая сессия остаётся на "{session_name}" (сессионный выбор /profile)'
        else:
            effect = (
                self._launch_profile_effect(name) or _MSG_APPLIES_NEXT_TURN
            )
        self._render_view()
        self._flash(
            f"Профиль «{name}» активирован — {_shown_path(target)}. {effect}"
        )

    # --- CRUD файлов профилей (волна B, Task 5) ---
    def _profiles_base_root(self):
        """Базовый корень профилей — единственная цель СОЗДАНИЯ (§12.3: проектный
        `.gc/profiles` остаётся ручной YAML-территорией, территорию не расширяем).
        Правило корней живёт в одном месте — `profile_roots` (проектный первым)."""
        import os
        from pathlib import Path

        from gc_core.config_modules.profiles import profile_roots

        return profile_roots(Path(os.getcwd()), self._profiles_base_path())[1]

    def _profiles_root_writable(self, ref=None) -> bool:
        """Гейт файловых операций секции — тот же guard пакетного шаблона, что у
        записи указателя (`_target_writable`), но по КОРНЮ профилей: базовый корень
        лежит рядом с базовым config.yml, и на пакетном шаблоне создание/удаление
        писало бы внутрь установленного пакета. Проектный корень — территория
        пользователя, он писуч всегда."""
        if ref is not None and ref.root_kind == "project":
            return True
        return self._target_writable(self._profiles_base_path())

    def _profile_row_by_name(self, name: str):
        """Строка профиля по имени либо None: набор перечитывается с диска на каждое
        нажатие, и файл мог исчезнуть между открытием ввода/баннера и подтверждением."""
        return next(
            (
                r
                for r in self._profiles_rows()
                if r["kind"] == "profile" and r["name"] == name
            ),
            None,
        )

    def _base_config_doc(self) -> dict:
        """Сырой документ базового config.yml через кэш прохода — симметрично
        `_project_config_doc`: указатель `profile:` живёт в обоих файлах, и
        спрашивают их одни и те же места."""
        cache = self._profiles_disk_cache
        if "base_doc" not in cache:
            from gc_core import config_raw

            cache["base_doc"] = config_raw.read_document(
                self._profiles_base_path()
            )
        return cache["base_doc"]

    def _profile_pointer_layers(self, name: str) -> list:
        """Файловые слои, чей указатель `profile:` называет `name` (проектный
        первым — порядок победы при merge).

        Спрашивать «активен ли профиль» у merged-конфига (`_config_profile()`)
        для файловых операций НЕЛЬЗЯ: там виден только слой-победитель, а `profile:`
        стоит в обоих файлах независимо. Базовый конфиг назвал `work`, проектный
        перекрыл на `corp` — merged отвечает «work не активен», и переименование/
        удаление оставляло бы базовый указатель висеть на исчезнувшем имени. Первый
        же запуск ВНЕ проекта (проектного слоя нет) падал бы «профиль work не
        найден» — ровно тот отказ §3.1, ради недопущения которого сделан порядок
        «указатель → файл» с откатом.

        Machine-managed оверлей (`config.local.yml`) здесь не спрашивается — его не
        знает и правило записи волны A (`_profile_activation_target`): указатель
        туда не пишет никто, а знание о слое, размазанное по двум правилам, хуже
        одного (пробел зафиксирован в отчёте)."""
        from gc_core.config_modules.layers import resolve_pointer_value

        def _named(doc: dict) -> bool:
            # Значение интерполируется ПЕРЕД сравнением ТЕМ ЖЕ домом, что ядро
            # (`layers.resolve_pointer_value`): загрузчик разворачивает
            # `${ENV:…}` до чтения указателя, и сырое сравнение не опознавало
            # слой-носитель при `profile: "${ENV:X}"`. Своя редакция на полном
            # резолвере шумела warning'ом и лезла на диск на каждый кадр баннера
            # и расходилась с ядерным читателем в том же кадре (ревью волны F,
            # находки 2–3).
            return resolve_pointer_value(doc.get("profile")) == name

        layers = []
        if _named(self._project_config_doc()):
            layers.append(self._project_config_candidate_path())
        if _named(self._base_config_doc()):
            layers.append(self._profiles_base_path())
        return layers

    def _profile_pointer_pinned_elsewhere(self, name: str, layers: list):
        """Файл-носитель указателя `profile: <name>`, который операция НЕ перепишет
        (managed `config.local.yml` — слой, которого правило записи осознанно не
        знает), либо None. Правило записи здесь НЕ расширяется: снять чужой
        machine-managed указатель мы не вправе — но молчание оставляло бы после
        удаления/переименования ссылку на исчезнувшее имя, и следующий старт не
        собирался бы. Предупреждение — на входе, вместе с остальными следствиями.

        Вопрос задаётся ПОДСТАНОВКОЙ — готовым приёмом лестницы починки
        (`onboarding.load_config_for_repair`): в стек кандидатами кладутся
        документы переписываемых слоёв со снятым указателем, и тот же резолв,
        каким указатель читает следующий старт (`resolve_profile_pointer`),
        отвечает, переживёт ли имя операцию; носителя называет та же витрина
        происхождения (`origins`) по тому же стеку. Своя прикидка «а не лежит ли
        он в managed» была бы четвёртой редакцией резолва указателя.

        Документы слоёв — из кэша прохода (`_base_config_doc`/`_project_config_doc`),
        не с диска: `layers` — ровно те пути, что вернул `_profile_pointer_layers`
        секундой раньше, спросив те же самые файлы (находка ревью #11). Баннер
        удаления/переименования зовёт обе функции подряд на каждой отрисовке —
        свежее чтение здесь поверх уже тёплого кэша удваивало read+parse YAML на
        UI-потоке без всякой причины (кэш и так живёт один проход)."""
        from gc_core import config_raw
        from gc_core.config_modules.layers import resolve_profile_pointer
        from gc_core.config_origin import origins

        base = self._profiles_base_path()
        cached = {
            self._project_config_candidate_path(): self._project_config_doc(),
            base: self._base_config_doc(),
        }
        candidates = {}
        for target in layers:
            source = cached.get(target)
            doc = (
                dict(source)
                if source is not None
                else dict(config_raw.read_document(target))
            )
            doc["profile"] = ""
            candidates[target] = doc
        if resolve_profile_pointer(base, candidates=candidates) != name:
            return None
        survivor = origins(
            str(base), ["profile"], candidates=candidates, profile_override=""
        )["profile"]
        return survivor.overlay_path or str(base)

    def _repoint_profile_layers(
        self, layers: list, value: str, *, extra_candidates=None
    ) -> tuple[list, Exception | None]:
        """Записать `profile: <value>` в каждый слой. Возвращает (записанные,
        ошибка | None) — откат частичной записи на вызывающем: только он знает,
        каким глаголом назвать неслучившееся действие. Ошибка отдаётся САМА, не
        текстом: её род (`_pointer_write_failure`) решает, что показать, а строка
        его стирает.

        `extra_candidates` пробрасывается в каждую запись как есть (см.
        `_write_profile_pointer`) — переименование подставляет им будущий
        документ профиля на его будущем пути."""
        written = []
        for target in layers:
            try:
                self._write_profile_pointer(
                    target, value, extra_candidates=extra_candidates
                )
            except (ValueError, OSError) as exc:
                return written, exc
            written.append(target)
        return written, None

    def _pointer_write_failure(
        self, done: str, action: str, exc: Exception
    ) -> str:
        """Текст несостоявшейся записи указателя `profile:` — по РОДУ сбоя.
        `OSError` — файл не записался (права, диск): честная файловая ошибка, как у
        включения/снятия рядом; `ValueError` — стек не собрался: отказ фазы 2 с
        адресом починки (`_profile_stack_write_refusal`). Одним текстом на оба
        рода PermissionError звучал как «конфиг стал бы нерабочим — поправьте в
        разделе «Модели»» и отправлял чинить реестр там, где не хватило прав."""
        if isinstance(exc, OSError):
            return f"не удалось {action}: {exc}"
        return self._profile_stack_write_refusal(done, exc)

    def _profile_name_error(
        self, name: str, *, exclude: str | None = None
    ) -> str | None:
        """Имя годится для создания/переименования — иначе текст ошибки. `exclude` —
        текущее имя при переименовании (сам себе тёзкой не считается).

        Разделители пути отклоняем отдельно от прочего: имя идёт прямо в имя файла
        (`<имя>.yml`), и это единственная защита от выхода за пределы корня.

        По той же причине — и запрещённые в именах файлов Windows символы. Без них
        человек получал в лицо сырое «[Errno 22] Invalid argument» с временным путём
        вместо ответа про имя, а `имя:поток` вдобавок оставлял в каталоге профилей
        мусорный файл `имя`: NTFS читает двоеточие как альтернативный поток и заводит
        под него носитель. Отвергаем на всех платформах: профиль, созданный на Linux,
        едет вместе с проектом, и там же он и ломается."""
        import os
        from pathlib import Path

        from gc_core.config_modules.model_registry import RESERVED_WORDS
        from gc_core.config_modules.profiles import (
            discover_all_refs,
            profile_roots,
        )

        if not name:
            return "введите имя профиля"
        if "/" in name or "\\" in name or name in (".", ".."):
            return "имя не должно содержать разделители пути"
        forbidden = sorted(set(name) & set(':*?"<>|'))
        if forbidden:
            return f"имя не должно содержать недопустимые символы ({' '.join(forbidden)})"
        if name in RESERVED_WORDS:
            return (
                f"имя «{name}» зарезервировано ({', '.join(RESERVED_WORDS)})"
            )
        roots = profile_roots(Path(os.getcwd()), self._profiles_base_path())
        # Тёзка ищется БЕЗ учёта регистра: имя идёт прямо в имя файла, а Windows и
        # macOS регистр не различают — побуквенное сравнение пропускало «TestProf1»
        # при живом «testprof1», и дальше падал уже `rename` с `[WinError 183]`,
        # показывая человеку путь вместо ответа про имя (а указатели к тому моменту
        # успевали переписаться, и их приходилось откатывать). Отвергаем на ВСЕХ
        # платформах — по той же причине, что и запрещённые символы Windows выше:
        # профиль едет вместе с проектом и ломается уже на чужой машине.
        existing = {
            known.casefold(): known for known in discover_all_refs(roots)
        }
        clash = existing.get(name.casefold())
        # Сам себе тёзкой не считается: сменить РЕГИСТР своего имени законно, и
        # файловая система такое переименование принимает.
        if clash is not None and clash != exclude:
            return f"профиль «{clash}» уже существует"
        return None

    def _profile_write_block(
        self, *, ref=None, name: str | None = None
    ) -> str | None:
        """Что мешает файловой операции над профилем — причина отказа либо None.

        Предикат ОДИН на дверь (`n`/`r`/`Del`) и на саму операцию: разойдясь, они
        дают ложное обещание — поле ввода/баннер поднимаются лишь затем, чтобы
        отказать после ответа. Проверяются оба объекта, и они разные: каталог
        профилей (туда ляжет файл) и файл конфига, чей указатель придётся
        переписать (`_profile_pointer_layers`). Второй бывает непишущимся при
        писучем первом — проектный профиль, названный базовым конфигом."""
        if not self._profiles_root_writable(ref):
            return "каталог профилей недоступен для записи"
        for target in self._profile_pointer_layers(name) if name else []:
            if not self._target_writable(target):
                return f"файл конфига {target} недоступен для записи"
        return None

    def _profile_write_refusal(self, done: str, reason: str) -> str:
        return self._PROFILE_FILE_WRITE_REFUSAL.format(
            done=done, reason=reason
        )

    def _profile_stack_write_refusal(
        self, action: str, detail, *, shown_path=None
    ) -> str:
        """Отказ фазы 2 при записи указателя `profile:` (`_write_profile_pointer`) —
        называет несостоявшееся действие и куда идти чинить, деталь остаётся
        сырой строкой валидатора. Общая формулировка верна для ЛЮБОЙ причины
        отказа фазы 2 (не только «нет slots.default» — так же и слот на алиас,
        живший в снятом/переименованном профиле, и привязки `assign`), поэтому
        разбирать причину здесь не нужно — деталь сама называет её.

        Без "✗": решение о нём — за вызывающим (`_rollback_profile_pointers`
        собирает деталь `file_exc` СЮДА один раз для нескольких путей записи, и
        второй "✗" от этой функции задваивался бы)."""
        from gc_core.config_modules.layers import StackAssemblyError

        if isinstance(detail, StackAssemblyError):
            # Стек не собрался — значит дело в самих ФАЙЛАХ (кодировка, YAML,
            # указатель в никуда), а не в реестре. Звать в «Модели» тут значит
            # послать не по адресу; деталь ниже уже называет файл и причину.
            # Путь этого профиля витрина печатает первой строкой блока под
            # курсором, а причину — второй: повторяя обе, отказ давал их на
            # экране трижды. `shown_path` задан — значит вызывающий знает,
            # что показано, и повтор снимается.
            return (
                f"профиль не {action} — конфиг стал бы нерабочим:\n  {_without_path_prefix(str(detail), shown_path)}"
                if shown_path is not None
                else f"профиль не {action} — конфиг стал бы нерабочим:\n  {detail}"
            )
        return f"профиль не {action} — конфиг стал бы нерабочим. Поправьте в разделе «Модели» и повторите:\n  {detail}"

    def _begin_profile_name_prompt(
        self, mode: str, original: str | None = None
    ) -> None:
        """Поднять инлайн-ввод имени (контракт п.5): поле раскрывается на месте —
        под своей строкой при переименовании, под списком при создании."""
        if mode == "rename" and self._refuse_profile_switch():
            return
        ref = (
            None
            if original is None
            else (self._profile_row_by_name(original) or {}).get("ref")
        )
        block = self._profile_write_block(ref=ref, name=original)
        if block:
            self._notice(
                self._profile_write_refusal(
                    "переименовано" if mode == "rename" else "создано", block
                )
            )
            return
        self._profiles_name_prompt = {"mode": mode, "original": original}
        self._render_view()

    def _cancel_profile_name_prompt(self) -> None:
        self._profiles_name_prompt = None
        self._render_view()

    def _submit_profile_name_prompt(self) -> None:
        prompt = self._profiles_name_prompt
        try:
            value = self.query_one("#set-profile-name", Input).value.strip()
        except NoMatches:
            return
        if prompt["mode"] == "rename":
            self._rename_profile(prompt["original"], value)
        else:
            self._create_profile(value)

    def _reload_after_profile_file_change(self, done: str) -> bool:
        """Перечитать конфиг после файловой операции над профилем — та же
        последовательность, что у активации рядом. Перечитка нужна и когда профиль
        не был активен: сессионный выбор (`/profile`) мог указывать именно на этот
        файл, и санацию (§9.2) делает именно она (`app.reload_config` →
        `sanitize_session_profile` + notice). False — перечитать не удалось, о чём
        уже сказано notice'ом (сама операция при этом состоялась)."""
        try:
            self._on_apply()
            self._pull_base_from_app()
        except Exception as exc:
            self._render_view()
            self._notice(f"{done}, но конфиг не перечитан: {exc}")
            return False
        self._raw = self._read_raw()
        self._origins = self._read_origins()
        return True

    def _create_profile(self, name: str) -> None:
        """`n` → имя → файл из `PROFILE_SKELETON` (бланк с шапкой-инструкцией, а НЕ
        учебный пример пустого состояния: у того литеральный токен и чужой адрес).
        Курсор встаёт на новую строку, и профиль сразу ОТКРЫВАЕТСЯ в редакторе:
        имя дают, чтобы файл заполнить, — заполнять идут следующим действием."""
        from gc_core import onboarding_config_writer as ocw
        from gc_core.config_modules.profiles import PROFILE_SKELETON

        error = self._profile_name_error(name)
        if error:
            self._notice(f"✗ не создано: {error}")
            return
        path = self._profiles_base_root() / f"{name}.yml"
        try:
            ocw.create_profile_file(path, PROFILE_SKELETON)
        except (OSError, ValueError) as exc:
            self._notice(f"✗ не создано: {exc}")
            return
        self._profiles_name_prompt = None
        rows = self._profiles_rows()
        row = next(
            (r for r in rows if r["kind"] == "profile" and r["name"] == name),
            None,
        )
        if row is not None:
            self._profiles_idx = rows.index(row)
            # Открытие рисует само. Отказать оно здесь не может: дверь и создание
            # спрашивают ОДИН предикат об одном и том же корне, а только что
            # записанный бланк читается. Ветка `else` — про НЕнайденную строку
            # (файл исчез между записью и перечиткой), а не про отказ.
            self._open_profile(row)
        else:
            self._render_view()
        self._flash(f"Профиль «{name}» создан — {path}")

    def _session_profile_name(self) -> str | None:
        return (getattr(self.app, "_session_overrides", None) or {}).get(
            "profile"
        )

    def _launch_profile_effect(self, pointer: str) -> str | None:
        """Эффект записанного указателя `pointer` для ТЕКУЩЕГО ЗАПУСКА под флагом
        `--profile` (`TuiApp._profile_override`, `""` — «без профиля»): флаг переживает
        перечитывание, и записанное применится лишь со следующего запуска без
        него. «Применится со следующего хода агента» здесь — та же ложь, что и под
        сессионным выбором. None — флага нет либо он называет то же имя.

        `_profile_override_cleared_name` (ревью #10): удаление профиля флага пишет
        `""` не потому, что человек запускался с `--profile ""`, а потому, что
        перечитка (`_load_base_config`) обязана видеть именно эту раскладку —
        `None` она бы приняла за «флага нет вовсе» и вернулась бы к указателю из
        файла. Текст об этом не знал ничего, кроме итогового `""`, и после
        удаления «work» с живым тёзкой в другом корне (флеш зовёт «включить —
        Space») называл флаг пустым, хотя он называл «work» — тот же класс лжи,
        которого текст обязан избегать."""
        launch = getattr(self.app, "_profile_override", None)
        if launch is None or launch == pointer:
            return None
        if launch:
            return f"текущий запуск остаётся на «{launch}» (флаг --profile)"
        cleared = getattr(self.app, "_profile_override_cleared_name", None)
        if cleared:
            return f"текущий запуск остаётся без профиля — профиль «{cleared}» из флага --profile удалён в этом сеансе"
        return 'текущий запуск остаётся без профиля (флаг --profile "")'

    def _profile_session_effect(
        self, before: str | None, *, pointer_changed: bool, pointer: str = ""
    ) -> str:
        """Эффект операции для ТЕКУЩЕЙ сессии — в плашку, а не только в `/notices`
        (канон волны A, `_activate_profile`). Считается ПОСЛЕ перечитки: снял ли
        сессионный выбор санатор (§9.2) и куда он переехал, видно только по факту.

        `pointer_changed` — переписан ли хоть один действующий указатель. Без него
        хвост «применится со следующего хода агента» вставал бы и там, где для
        агента не изменилось НИЧЕГО (переименован профиль, на который не ссылается
        ни один слой), — тот же класс лжи, за который переписывались футеры. Нечего
        сказать — пустая строка, а не вежливая неправда. `pointer` — что теперь
        записано в указателе (новое имя у переименования, пусто у удаления): под
        флагом запуска обещание про следующий ход уступает место эффекту флага."""
        after = self._session_profile_name()
        if after and after != before:
            return f"сессионный выбор /profile переехал на «{after}»"
        if after:
            return f"текущая сессия остаётся на «{after}» (сессионный выбор /profile)"
        if before:
            acting = self._config_profile() or "без профиля"
            return f"сессионный выбор «{before}» снят — действует «{acting}»"
        if not pointer_changed:
            return ""
        launch = self._launch_profile_effect(pointer)
        if launch:
            return launch
        if not pointer:
            # Указатель снят В ПУСТО (удаление): «применится …» без предмета в
            # плашке рядом со строкой о тёзке читалось как «применится тень» —
            # снятие называется снятием, обещание про следующий ход остаётся.
            return "Выбор снят: со следующего хода агента — без профиля."
        return _MSG_APPLIES_NEXT_TURN

    def _rename_profile(self, original: str, new_name: str) -> None:
        """`r` → новое имя: переименовываются файл И указатель в КАЖДОМ файловом
        слое, который называет старое имя (`_profile_pointer_layers`).

        Порядок: указатели ПЕРЕД файловой операцией. Обратный оставлял бы висячий
        `profile: <старое имя>` без файла при сбое записи указателя ПОСЛЕ уже
        состоявшегося rename, а холодный старт с нерезолвимым указателем — строгая
        ошибка (§3.1).

        Указатель при этом пишется на НОВОЕ имя, а файл профиля физически ещё
        лежит под СТАРЫМ — `resolve_profile` в момент валидации указателя новое
        имя не находит вовсе (найдётся только после `rename()` ниже). Без
        `profile_candidate` (будущий документ профиля НА ЕГО будущем пути,
        содержимое то же — переименование его не меняет) фаза 2 резолвила бы
        стек БЕЗ профиля и отклоняла бы рабочую раскладку «модели в базе, слот
        в профиле» (найдено ревью: регресс более ранней правки этой сессии)."""
        if new_name == original:
            self._cancel_profile_name_prompt()  # ничего не изменилось — тихая отмена
            return
        error = self._profile_name_error(new_name, exclude=original)
        if error:
            self._notice(f"✗ не переименовано: {error}")
            return
        if profile_switch_refusal(self.app) is not None:
            self._profiles_name_prompt = None
            self._refuse_profile_switch(rerender=True)
            return
        row = self._profile_row_by_name(original)
        if row is None:
            self._cancel_profile_name_prompt()  # файл исчез под ногами — показать как есть
            return
        ref = row["ref"]
        block = self._profile_write_block(ref=ref, name=original)
        if block:
            self._notice(self._profile_write_refusal("переименовано", block))
            return
        layers = self._profile_pointer_layers(original)
        session_before = self._session_profile_name()
        from gc_core.config import _raw_from_text

        new_profile_path = ref.path.with_name(f"{new_name}.yml")
        # Ничего ещё не записано — откатывать нечего, достаточно отказа с
        # внятным текстом (тот же приём, что и у соседних файловых точек этой
        # функции: `ref.path.rename` ниже и запись указателя ловят свои
        # `OSError`/`ValueError` так же, не давая им уйти необработанными).
        #
        # Отказ называет ФАЙЛ, форматом «<путь>: не удалось прочитать: …» — тем
        # же, каким виноватый файл называют чтение слоёв и объяснение сломанного
        # конфига. Голой жалобы кодека («'utf-8' codec can't decode byte 0xff» на
        # профиле, сохранённом в UTF-16) человеку не хватает: он не знает, какой
        # из профилей чинить.
        from gc_core.utils.user_text import read_user_text

        try:
            profile_text = read_user_text(ref.path)
        except ValueError as exc:
            self._notice(f"✗ не переименовано: {exc}")
            return
        try:
            profile_candidate = {
                new_profile_path: _raw_from_text(profile_text, "профиль")
            }
        except ValueError as exc:
            self._notice(f"✗ не переименовано: {ref.path}: {exc}")
            return
        # Занятость цели судит ФАЙЛОВАЯ СИСТЕМА, не дискавери: гейт тёзок выше
        # сравнивает ИМЕНА из дискавери, а `rename()` работает с ПУТЯМИ, и цель,
        # которой в дискавери нет (резервное имя вроде `default.yml`, тёзка по
        # регистру на регистрочувствительной ФС), POSIX-rename перезаписал бы
        # МОЛЧА и без бэкапа. `samefile` оставляет законной смену регистра
        # СОБСТВЕННОГО имени: на регистронечувствительной ФС `exists()` находит
        # сам переименовываемый файл. Гейт стоит ДО `_repoint_profile_layers` —
        # иначе там, где ФС откажет сама (Windows), указатели `profile:` успевали
        # бы переписаться и откатиться впустую.
        if new_profile_path.exists() and not new_profile_path.samefile(
            ref.path
        ):
            self._notice(
                f"✗ не переименовано: файл {new_profile_path} уже занят"
            )
            return
        written, error = self._repoint_profile_layers(
            layers, new_name, extra_candidates=profile_candidate
        )
        if error is not None:
            self._rollback_profile_pointers(
                written,
                original,
                file_exc=self._pointer_write_failure(
                    "переименован", "переименовать", error
                ),
                action="переименовать",
            )
            return
        try:
            ref.path.rename(ref.path.with_name(f"{new_name}.yml"))
        except OSError as exc:
            self._rollback_profile_pointers(
                written,
                original,
                file_exc=f"не удалось переименовать: {exc}",
                action="переименовать",
            )
            return
        self._profiles_name_prompt = None
        if session_before == original:
            # Переименование — не исчезновение: сессионный выбор ЕДЕТ за именем
            # (ruling координатора), а не снимается санацией §9.2. Через
            # `apply_session_override`, а не голый set: у удаления санация доходит
            # до meta треда, и переезд обязан доходить туда же — иначе возврат в
            # тред воскресил бы старое имя, и его снял бы уже санатор.
            try:
                self.app.apply_session_override("profile", new_name)
            except OSError:
                pass  # meta недоступна: оверрайд откачен, дальше его снимет санация
        if getattr(self.app, "_profile_override", None) == original:
            # Флаг запуска `--profile` едет за именем по той же причине: иначе
            # перечитка ниже не нашла бы профиль флага и сняла бы его санацией —
            # с уведомлением о пропаже профиля, который только что переименовали.
            self.app._profile_override = new_name
        if not self._reload_after_profile_file_change("переименовано"):
            return
        # Курсор — на переименованную строку (приём создания): список сортирован,
        # новое имя встаёт в другую позицию, и прежний индекс указывал бы на
        # СОСЕДНИЙ профиль — следующий Space активировал бы его без подтверждения.
        rows = self._profiles_rows()
        row = next(
            (
                r
                for r in rows
                if r["kind"] == "profile" and r["name"] == new_name
            ),
            None,
        )
        if row is not None:
            self._profiles_idx = rows.index(row)
        self._render_view()
        self._flash(
            " ".join(
                filter(
                    None,
                    [
                        f"Профиль «{original}» переименован в «{new_name}».",
                        self._profile_session_effect(
                            session_before,
                            pointer_changed=bool(layers),
                            pointer=new_name,
                        ),
                    ],
                )
            )
        )

    def _request_delete_profile(self, name: str) -> None:
        if self._refuse_profile_switch():
            return
        row = self._profile_row_by_name(name)
        if row is not None:
            # Отказ НА ВХОДЕ, тем же предикатом, что и сама операция: поднимать
            # вопрос, на который заведомо ответишь отказом, — ложное обещание.
            block = self._profile_write_block(ref=row["ref"], name=name)
            if block:
                self._notice(self._profile_write_refusal("удалено", block))
                return
        self._profiles_delete_name = name
        self._render_view()

    def _cancel_delete_profile(self) -> None:
        self._profiles_delete_name = None
        self._render_view()

    def _profile_delete_confirm_text(self) -> str:
        """Баннер называет ВСЕ необратимые следствия заранее: файл, каждый файловый
        указатель (их бывает больше одного — см. `_profile_pointer_layers`) и
        сессионный выбор, который снимет санация."""
        name = self._profiles_delete_name or ""
        parts = [
            f"Удалить профиль «{name}»? Файл уедет в backups/ рядом с ним."
        ]
        layers = self._profile_pointer_layers(name)
        if layers:
            active = (
                " (сейчас активен)" if self._config_profile() == name else ""
            )
            parts.append(
                f"Указатель на него{active} снимется в: "
                + ", ".join(str(t) for t in layers)
                + "."
            )
        pinned = self._profile_pointer_pinned_elsewhere(name, layers)
        if pinned:
            # Слой, который правило записи не переписывает (managed), продолжит
            # называть исчезнувший файл — предупредить обязаны ДО Enter.
            parts.append(
                f"Указатель на профиль лежит в {pinned} — снимите его там, иначе следующий запуск не соберётся."
            )
        if self._session_profile_name() == name:
            parts.append("Сессионный выбор /profile тоже снимется.")
        if getattr(self.app, "_profile_override", None) == name:
            # Флаг --profile называет удаляемый профиль — удаление его снимет
            # (см. `_delete_profile`), и запуск переедет: следствие необратимое,
            # значит место ему здесь, до Enter.
            parts.append(
                "Профиль выбран флагом --profile — запуск перейдёт на «без профиля»."
            )
        parts.append("Enter — удалить · Esc — отмена")
        return " ".join(parts)

    def _confirm_delete_profile(self) -> None:
        name = self._profiles_delete_name
        self._profiles_delete_name = None
        if name is None or self._refuse_profile_switch(rerender=True):
            return
        self._delete_profile(name)

    def _delete_profile(self, name: str) -> None:
        """Enter в confirm-баннере: файл уезжает в бэкап конвенцией `config_sync`
        (`backups/<файл>.bak-<ts>`, решение юзера В2), плашка называет путь.

        Порядок тот же, что у переименования, и по той же причине: указатель
        снимается в КАЖДОМ файловом слое, который называет профиль
        (`_profile_pointer_layers`), ПЕРЕД перемещением файла; сбой перемещения
        откатывает все переписанные указатели обратно."""
        import shutil

        row = self._profile_row_by_name(name)
        if row is None:
            self._render_view()  # файл исчез под ногами — просто перерисовать
            return
        ref = row["ref"]
        block = self._profile_write_block(ref=ref, name=name)
        if block:
            self._notice(self._profile_write_refusal("удалено", block))
            return
        layers = self._profile_pointer_layers(name)
        session_before = self._session_profile_name()
        shadow = row.get("shadow")
        written, error = self._repoint_profile_layers(layers, "")
        if error is not None:
            self._rollback_profile_pointers(
                written,
                name,
                file_exc=self._pointer_write_failure(
                    "удалён", "удалить", error
                ),
                action="удалить",
            )
            return
        path = ref.path
        backup = self._profile_backup_path(path)
        try:
            # mkdir — в ТОМ ЖЕ try, что и move: указатели к этому моменту уже
            # переписаны, и сбой создания каталога обязан откатывать их так же,
            # как сбой самого перемещения.
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(backup))
        except OSError as exc:
            self._rollback_profile_pointers(
                written,
                name,
                file_exc=f"не удалось удалить: {exc}",
                action="удалить",
            )
            return
        # Рост каталога ограничивает владелец конвенции — тем же числом, что и у
        # бэкапов конфига. Своей редакции правила здесь не заводим: разошлась бы с
        # обещанием readme на первой же правке одной из сторон.
        from gc_core.config_sync import prune_backups

        prune_backups(backup.parent, path.name)
        if getattr(self.app, "_profile_override", None) == name:
            # Флаг запуска называет только что удалённый профиль: оставленный, он
            # ронял бы КАЖДУЮ перечитку до конца сессии («профиль не найден») —
            # начиная с той, что строкой ниже. Пустая строка — «этот запуск без
            # профиля», раскладку уже понимает `_load_base_config`; переименование
            # рядом везёт флаг за именем по той же причине.
            #
            # `_profile_override_cleared_name` — ЧТО флаг называл до этой строки:
            # без него `_launch_profile_effect` не отличал бы «`""` — это удаление
            # сняло флаг с исчезнувшего профиля» от «человек и правда запустился
            # `--profile ""»`, и текст приписывал бы пустой флаг, которого не было
            # (ревью #10) — особенно заметно, когда тёзка того же имени жив в
            # другом корне и «включить — Space» тут же зовёт этот текст снова.
            self.app._profile_override_cleared_name = name
            self.app._profile_override = ""
        if not self._reload_after_profile_file_change("удалено"):
            return
        self._profiles_idx = 0
        self._render_view()
        parts = [f"Профиль «{name}» удалён — бэкап: {backup}."]
        # Эффект для сессии — ПЕРЕД строкой о тёзке: хвост «со следующего хода…»
        # после неё прирастал к тени и читался как «применится тень» (L24).
        parts.append(
            self._profile_session_effect(
                session_before, pointer_changed=bool(layers)
            )
        )
        if shadow is not None:
            # Удалён победивший тёзка: строка с тем же именем остаётся в списке
            # (из второго корня) — молчание читалось бы как «удаление не сработало».
            # «Включить — Space» отделяет резолвинг имени от действующего выбора:
            # тень НЕ применяется сама, её ещё надо включить.
            parts.append(
                f"Имя «{name}» теперь резолвится в {shadow.path}, включить — Space."
            )
        self._flash(" ".join(filter(None, parts)))

    @staticmethod
    def _profile_backup_path(path):
        """`backups/<файл>.bak-<ts>` (конвенция `config_sync`) + счётчик при занятом
        имени. Счётчик — не педантизм: `config_sync` КОПИРУЕТ, оригинал остаётся на
        месте, а здесь бэкап — ЕДИНСТВЕННАЯ копия удалённого файла, и второе
        удаление в ту же секунду затёрло бы первую безвозвратно."""
        import time

        backups = path.parent / "backups"
        stem = f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        candidate = backups / stem
        n = 2
        while candidate.exists():
            candidate = backups / f"{stem}-{n}"
            n += 1
        return candidate

    def _rollback_profile_pointers(
        self, layers: list, old_value: str, *, file_exc, action: str
    ) -> None:
        """Указатели уже переписаны, а файловая операция ПОСЛЕ них упала — вернуть
        каждый к тому, что действовало до начала операции. Иначе конфиг остаётся
        со ссылкой на несуществующее/непереименованное имя, а холодный старт с
        нерезолвимым `profile:` — строгая ошибка (§3.1).

        `file_exc` — уже готовый к показу текст (вызывающий решает: сырая ошибка
        файловой операции или обёрнутый отказ фазы 2, `_profile_stack_write_
        refusal`) — единственный "✗" на весь notice добавляется здесь.

        Откат сам не удался (`stuck`) — это АВАРИЙНОЕ восстановление после
        чужого сбоя не сработало, а не первая попытка: «поправьте и повторите»
        здесь неверно — повторять нечего, состояние уже смешанное. Формулировка
        называет это прямо и отправляет чинить руками, не обещая, что то же
        действие с той же кнопки сработает во второй раз."""
        stuck = []
        for target in layers:
            try:
                self._write_profile_pointer(target, old_value)
            except (ValueError, OSError) as rollback_exc:
                stuck.append(f"{target}: {rollback_exc}")
        if stuck:
            self._notice(
                f"✗ {action} не удалось, а автоматический откат указателя тоже "
                "не удался — конфиг может ссылаться на несуществующий профиль, "
                # Указатель `profile:` пишет секция «Профили», не «Модели»: слать
                # чинить в раздел, который этот ключ не правит, — послать не по
                # адресу. Файлы перечислены следом — «прямо в файле» вторая дверь.
                "поправьте вручную в разделе «Профили» или прямо в файле: "
                + "; ".join(stuck)
            )
            return
        self._notice(f"✗ {file_exc}")

    def _profiles_name_width(self, rows: list[dict]) -> int:
        """Колонка имени — общая у профилей и «без профиля»: колонка
        «активен» стоит сразу за ней у всех строк."""
        names = [r["name"] for r in rows if r["kind"] == "profile"]
        names.append(PROFILE_NONE_VALUE)
        return max(len(n) for n in names) + 3

    @staticmethod
    def _append_active_marker(t: Text, active: bool) -> None:
        """Колонка «активен» сразу за именем; у неактивной строки — пусто."""
        width = len(PROFILE_ACTIVE_STATUS) + _PROFILE_TAIL_GAP
        if active:
            t.append(PROFILE_ACTIVE_STATUS, style=OK)
            t.append(" " * _PROFILE_TAIL_GAP)
        else:
            t.append(" " * width)

    def _profile_row_text(
        self, row: dict, rows: list[dict], selected: bool
    ) -> ColumnRow:
        """Голова — маркер, имя, «активен», источник; хвост (слоты, секции,
        пометки выбора) переносится в своей колонке (дизайн-код §10)."""
        name, ref = row["name"], row["ref"]
        config_active = self._config_profile()
        name_w = self._profiles_name_width(rows)
        head = Text()
        head.append("▸ " if selected else "  ", style=ACCENT)
        head.append(f"{name:<{name_w}}", style=INK)
        self._append_active_marker(head, name == config_active)
        source = "проектный" if ref.root_kind == "project" else "глобальный"
        head.append(f"{source:<11}", style=DIM)
        atoms: list[Text] = []
        if (
            name == config_active
            and self._project_sets_profile_pointer()
            and ref.root_kind != "project"
        ):
            atoms.append(Text(" · выбран проектом", style=DEEP_DIM))
        summary = self._profile_summary(ref.path)
        slots = [f"{lvl} {alias}" for lvl, alias in summary.slots.items()]
        parts = [(" ", f"{s} ·") for s in slots[:-1]]
        parts += [(" ", s) for s in slots[-1:]]
        parts += [
            (" " if i == 0 else "", f"+{n}")
            for i, n in enumerate(summary.overlay_sections)
        ]
        # Отступ от колонки источника ставится ОДИН раз — перед первым куском
        # хвоста: привязанный к слотам, он пропадал у профиля с одними секциями,
        # и суффикс слипался в «глобальный+voice» (живой смок 2026-08-26).
        for i, (sep, part) in enumerate(parts):
            lead = " " * _PROFILE_TAIL_GAP if i == 0 else sep
            atoms.append(Text(lead + part, style=DIM))
        session_overrides = getattr(self.app, "_session_overrides", None) or {}
        session_name = session_overrides.get("profile")
        if session_name == name and session_name != config_active:
            atoms.append(Text("   · выбран в текущей сессии", style=DEEP_DIM))
        return ColumnRow(head, atoms)

    def _add_profile_row_text(self, selected: bool) -> Text:
        """Строка-действие «+ добавить профиль…» — перенос паттерна «Моделей»:
        Enter = клавиша n, цвет и грамматика те же (DIM + многоточие — пробы
        владельца: DEEP_DIM читался подсказкой, тил перетягивал глаз)."""
        t = Text()
        t.append("▸ " if selected else "  ", style=ACCENT)
        t.append("+ добавить профиль…", style=DIM)
        return t

    def _none_row_text(self, rows: list[dict], selected: bool) -> Text:
        """Строка «— без профиля»: глобальная деактивация; «активен» — когда
        конфиг-указатель пуст."""
        config_active = self._config_profile()
        name_w = self._profiles_name_width(rows)
        t = Text()
        t.append("▸ " if selected else "  ", style=ACCENT)
        t.append(PROFILE_NONE_VALUE.ljust(name_w), style=INK)
        self._append_active_marker(t, config_active is None)
        session_overrides = getattr(self.app, "_session_overrides", None) or {}
        if (
            session_overrides.get("profile") == ""
            and config_active is not None
        ):
            t.append("   · выбран в текущей сессии", style=DEEP_DIM)
        return t

    def _profiles_row_under_cursor(self, rows: list[dict]) -> dict:
        """Строка под курсором с клампом индекса: набор строк перечитывается с диска
        на каждый рендер и на каждое нажатие, а файл профиля мог исчезнуть под ногами
        между ними (правка/удаление извне) — курсор тогда указывает за конец."""
        self._profiles_idx = min(self._profiles_idx, len(rows) - 1)
        return rows[self._profiles_idx]

    def _profiles_hint_text(self, rows: list[dict]) -> str:
        return self._profiles_row_hint(self._profiles_row_under_cursor(rows))

    def _profiles_row_hint(self, row: dict) -> str:
        """Подсказка строки ``row`` секции «Профили»."""
        if row["kind"] == "add":
            # Полный путь — то же правило, что у «запись:» секции «Модели»: одно
            # имя не говорит, куда именно ляжет файл.
            return f"Enter — создать файл {self._profiles_base_root() / '<имя>.yml'}"
        if row["kind"] == "none":
            if self._config_profile() is None:
                # Единственная фраза секции о ДЕЙСТВУЮЩЕМ состоянии — остальное здесь
                # про файл. Сессионный выбор его перекрывает, и «действует чистый
                # конфиг» тогда прямая ложь: в силе профиль, который в это же время
                # показывает футер. Файловую половину оставляем — секция правит её.
                session = self._session_profile_name()
                if session:
                    return f"глобально профиль не выбран; в этой сессии действует «{session}»"
                return "профиль не выбран — действует чистый конфиг"
            return f"Space — деактивировать (запись: {self._deactivation_target()})"
        name, ref = row["name"], row["ref"]
        lines = [str(ref.path)]
        target = self._profile_activation_target(ref)
        lines.append(f"Space — активировать (запись: {target})")
        if row.get("shadow") is not None:
            # Имя профиля одно на систему: при совпадении в обоих корнях побеждает
            # проектный, а второй файл молча не участвует. Подсказка говорит именно
            # это — «есть, но не работает»: голое слово «тёзка» заставляло гадать,
            # кто кого перекрыл и в каком из двух файлов править (ревизия владельца).
            lines.append(
                f"тот же профиль есть здесь, но перекрыт: {row['shadow'].path}"
            )
        if (
            self._project_sets_profile_pointer()
            and self._config_profile() == name
            and ref.root_kind != "project"
        ):
            lines.append("выбор задан .gc/config.yml проекта")
        summary = self._profile_summary(ref.path)
        if summary.error:
            # Текст ядра самодостаточен («<путь>: причина») — он нужен таким тем,
            # кто печатает его в одиночку. Здесь путь уже стоит ПЕРВОЙ строкой
            # блока, и повтор давал его на экране дважды подряд: срезаем.
            lines.append(_without_path_prefix(summary.error, ref.path))
        elif summary.overlay_sections:
            lines.append("секции: " + ", ".join(summary.overlay_sections))
        return "\n".join(lines)

    def _mount_profiles_section(self, frame: Vertical) -> None:
        if self._profiles_open is not None and self._mount_open_profile(frame):
            return
        rows = self._profiles_rows()
        self._profiles_idx = min(self._profiles_idx, len(rows) - 1)
        profile_rows = [r for r in rows if r["kind"] == "profile"]
        if not profile_rows:
            frame.mount(
                Static(
                    "Профиль — отдельный файл-конфиг: переключаемый набор ЛЮБЫХ "
                    "настроек — модели, уровни, гварды и любые другие секции конфига",
                    classes=_CLS_SET_HINT_SET_DYN,
                )
            )
            # Ни клавиши, ни пути здесь больше нет — оба уже названы, и точнее:
            # клавишу обещает футер по позиции курсора (он стартует на
            # строке-действии), полный путь — строка под списком, и она считает его
            # от ФАКТИЧЕСКОГО корня профилей. Прежняя редакция звала «клавишей n»
            # рядом с футерным «Enter» и печатала захардкоженный
            # `~/.gc/profiles/...`, который врёт при своём GC_HOME и при запуске с
            # `--config`. Пример YAML снят следом: пустая секция — место для одного
            # действия, а не для справочника формата (он есть в
            # docs/configuration.md и demo_profile.yml).
        # Цикл идёт и в пустом состоянии: там остаётся одна «+ добавить профиль» —
        # строка-действие под подсказкой о сути профиля.
        for i, row in enumerate(rows):
            selected = i == self._profiles_idx
            cls = "set-row set-dyn set-profiles-row" + (
                _CLS_SELECTED if selected else ""
            )
            if row["kind"] == "none":
                text = self._none_row_text(rows, selected)
            elif row["kind"] == "add":
                text = self._add_profile_row_text(selected)
            else:
                text = self._profile_row_text(row, rows, selected)
            frame.mount(Static(text, classes=cls))
            if self._name_prompt_belongs_to(row):
                self._mount_profile_name_prompt(frame)
        if (self._profiles_name_prompt or {}).get("mode") == "create":
            # Создание своей строки не имеет — поле раскрывается ПОД СПИСКОМ
            # (контракт п.5): строка «+ добавить» последняя, и поле встаёт ровно
            # под ней; в пустом состоянии — под подсказкой-примером.
            self._mount_profile_name_prompt(frame)
        if self._profiles_name_prompt is None:
            # Во время ввода имени подсказка МОЛЧИТ: она про строку под курсором
            # («Space — активировать», путь файла), а клавиши забрал инпут — Space
            # там печатается. Обещание недоступного плюс лишние строки между полем
            # и ответом на него: отказ уезжал в конец секции, за чужой текст. Роль
            # клавиш в этом состоянии называет позиционный футер.
            # Высота — под самую длинную подсказку строк (дизайн-код §9).
            frame.mount(
                ReservedHint(
                    self._profiles_hint_text(rows),
                    variants=lambda: [
                        self._profiles_row_hint(r) for r in rows
                    ],
                    classes="set-hint set-dyn set-profiles-hint",
                )
            )
        if self._profiles_delete_name is not None:
            frame.mount(
                Static(
                    Text(self._profile_delete_confirm_text(), style=WARN),
                    classes=f"set-confirm set-dyn {KEEP_IN_VIEW}",
                    markup=False,
                )
            )

    def _name_prompt_belongs_to(self, row: dict) -> bool:
        """Строка, под которой раскрывается инлайн-ввод имени: своя строка профиля
        при переименовании (создание строки не имеет — см. `_mount_profiles_section`)."""
        prompt = self._profiles_name_prompt
        return (
            prompt is not None
            and prompt["mode"] == "rename"
            and row["kind"] == "profile"
            and row["name"] == prompt["original"]
        )

    def _mount_profile_name_prompt(self, frame: Vertical) -> None:
        """Инлайн-ввод имени (контракт п.5): поле без отдельной подписи — контекст
        даёт строка, под которой оно раскрылось; рамка — канон полей /settings."""
        from gc_tui.widgets.paste_input import PasteInput

        prompt = self._profiles_name_prompt
        prefill = prompt["original"] if prompt["mode"] == "rename" else ""
        inp = PasteInput(
            value=prefill or "",
            placeholder="имя профиля",
            id="set-profile-name",
            classes=f"set-dyn set-spaced set-framed {KEEP_IN_VIEW}",
        )
        frame.mount(inp)
        if prompt["mode"] == "rename":
            # Симметрия с баннером удаления (L23): указатель в слое, которого
            # правило записи не знает (managed), останется на СТАРОМ имени —
            # предупреждение стоит у поля, до Enter.
            name = prompt["original"]
            pinned = self._profile_pointer_pinned_elsewhere(
                name, self._profile_pointer_layers(name)
            )
            if pinned:
                frame.mount(
                    Static(
                        Text(
                            f"указатель на профиль лежит в {pinned} — поправьте его "
                            "там, иначе следующий запуск не соберётся",
                            style=WARN,
                        ),
                        classes=_CLS_SET_HINT_SET_DYN,
                        markup=False,
                    )
                )
        inp.focus()

    def _refresh_profiles_hint(self) -> None:
        if self._view != _PROFILES:
            return
        rows = self._profiles_rows()
        widgets = list(self.query(".set-profiles-hint"))
        if widgets:
            widgets[-1].update(self._profiles_hint_text(rows))

    # --- редактор открытого профиля (волна B, Task 6) ---
    #
    # Симметрия §1 спеки: формы одни, целевых файла два. Под-вид ВНУТРИ секции
    # «Профили» — тот же `ModelsSectionEditor`, но целью записи ему дан файл
    # профиля, а валидатором — профильный (`is_profile=True`). Экран здесь делает
    # ровно то же, что для секции «Модели»: монтирует, роутит клавиши и держит
    # витрины; логика редактора живёт в его модуле.
    _PROFILE_OVERLAY_HEAD = "профиль также переопределяет: "
    _PROFILE_OVERLAY_HINT_HEAD = "секции: "
    _PROFILE_OVERLAY_HINT_TAIL = " · правятся в YAML — форм для них здесь нет"

    def _open_profile(self, row: dict) -> None:
        """Enter на строке профиля: открыть под-вид редактора на его файле. Отказ —
        notice БЕЗ перерисовки (канон отказов секции: `_render_view` снёс бы своё же
        сообщение вместе с прочим динамическим содержимым).

        Гейт — тот же `_profile_write_block`, что у остальных файловых операций
        секции: редактор это поверхность ЗАПИСИ, и открывать его над файлом, куда
        запись заведомо не пойдёт, значит обещать ложно (урок N1 Task 5). Причину
        предикат называет сам. Битый YAML — второй такой случай: писать в него
        нельзя ни одной операцией (слой записи разбирает документ перед правкой),
        а показанный «пустой реестр» был бы враньём о содержимом файла."""
        block = self._profile_write_block(ref=row["ref"])
        if block:
            self._notice(self._profile_write_refusal("открыто", block))
            return
        error = self._profile_summary(row["ref"].path).error
        if error:
            # Путь и причина уже стоят в блоке под курсором той же строки —
            # отказ отвечает про ДЕЙСТВИЕ, а не пересказывает диагноз в третий раз.
            self._notice("✗ не открыто: файл профиля не читается")
            return
        self._profiles_open = row["name"]
        self._render_view()

    def _mount_open_profile(self, frame: Vertical) -> bool:
        """Тело под-вида. False — открытого профиля больше нет (файл переименовали
        или удалили извне, пока он был открыт): под-состояние снимается само, и
        секция рисует список — состояние экрана задаёт диск, а не история нажатий."""
        row = self._profile_row_by_name(self._profiles_open)
        if row is None:
            self._profiles_open = None
            return False
        from gc_tui.editors.models_section import ModelsSectionEditor

        frame.mount(
            Static(
                "",
                classes="set-info set-dyn set-profiles-overlay",
                markup=False,
            )
        )
        frame.mount(
            Static(
                "",
                classes="set-hint set-dyn set-profiles-overlay-hint",
                markup=False,
            )
        )
        self._refresh_profile_overlay()
        # Стек ОТКРЫТОГО профиля (None — открыт активный: его стек и есть runtime).
        # Всё, что описывает его подключение, берётся отсюда
        # одним куском: адрес с токеном для наследования и проб — и список пресетов
        # адреса, который профиль вправе переопределить тем же `endpoint_presets`.
        # Развести их значило бы чинить один случай правила и оставить второй.
        profile_cfg = self._open_profile_config(row["name"])
        endpoint, auth, verify, _m, proxy = self._probe_args_from_config(
            profile_cfg
        )
        frame.mount(
            ModelsSectionEditor(
                target=row["ref"].path,
                template="",
                is_profile=True,
                # Секрет-политика литеральных токенов действует для профиля ПРОЕКТНОГО
                # корня — признак от дискавери (`root_kind`), тем же условием, каким
                # загрузчик включает warn_literal_tokens (`layers.py`).
                project_profile=row["ref"].root_kind == "project",
                base_registry=self._base_registry_snapshot,
                # Пока профиль активен, его файл — часть стека следующего старта, и гейт
                # записи обязан судить merged; для неактивного валидатор сам остаётся на
                # фазе 1, сверив указатель базы с целью.
                stack_base=self._config_path,
                effective_url=endpoint,
                effective_auth=auth,
                verify=verify,
                config_proxy=proxy,
                presets=self._presets_of(profile_cfg),
                # Стек профиля не собрался — документный ответ несёт одно
                # подключение, раздела сжатия в нём нет.
                global_config=(
                    profile_cfg
                    if profile_cfg is not None
                    and not isinstance(profile_cfg, dict)
                    else self._settings
                ),
                active_choice=self._acting_choice(),
                on_change=self._models_changed,
                on_notice=self._models_notice,
            )
        )
        return True

    _VIEW_FILE_KEYS = ("v", "м", "V", "М")

    def _route_profile_file_view(self, ed, event: events.Key) -> None:
        """`v`/`м` — файл профиля в просмотрщике (§11a). Клавиша достаётся сюда
        только тем, что редактор её не взял (роутинг реестра вернул False).

        Гейты — все про «обещанное работает». Несущий здесь один: под висящим
        под-состоянием (форма, пикер, вопрос) клавиша молчит вместе с футером —
        варианта с `v` у тех состояний не заведено. При выключенном просмотрщике
        (`tui.file_view.enabled: false`) она не работает и не обещается вовсе —
        канон «недоступное не обещаем», а не отказ-нотис на обещание, которого
        экран не давал.

        `not isinstance(focused, Input)` — ЗАЩИТНЫЙ, а не несущий (проверено
        мутацией финального ревью: без него ни один страж не краснеет, поля с
        Input в под-виде профиля уже отсечены `showing_list`). Стоит каноном
        клавиш-объектов §5.3 — на случай, если под-вид обзаведётся полем вне
        формы."""
        if event.key not in self._VIEW_FILE_KEYS or isinstance(
            self.focused, Input
        ):
            return
        if not (ed.showing_list and self._file_view_settings().enabled):
            return
        event.stop()
        self._open_profile_file()

    def _open_profile_file(self) -> None:
        """Файл открытого профиля в read-only просмотрщике поверх хаба: немодельные
        секции правятся только в YAML (идеология «пульт / машинное отделение»), но
        СМОТРЕТЬ их можно отсюда. Профиль исчез с диска между кадрами — открывать
        нечего, и молчание здесь честнее нотиса: список перечитается сам."""
        row = self._profile_row_by_name(self._profiles_open)
        if row is None:
            return
        from gc_tui.screens.file_view import FileViewerScreen
        from gc_tui.services.file_view import ViewPositionStore

        store = getattr(self.app, "view_positions", None)
        if store is None:
            store = ViewPositionStore()
            self.app.view_positions = store
        path = row["ref"].path
        self.app.push_screen(
            FileViewerScreen(
                raw_path=str(path),
                cwd=str(path.parent),
                store=store,
                settings=self._file_view_settings(),
            )
        )

    def _base_registry_snapshot(self) -> dict:
        """Тень основного конфига для под-вида профиля: что действует в строках
        реестра и уровней, пока профиль своего не говорит (§3 спеки эффективного
        вида). `{"models", "slots", "assign", "base_url"}`.

        Источник — ДОКУМЕНТ базового config.yml (цель основной секции), а не
        merged база окна: она собрана с УЖЕ АКТИВНЫМ профилем, и тень стала бы
        гибридной — открытый профиль A показывал бы «основной конфиг» через оверлей
        активного профиля B. По той же причине адрес берётся отсюда, а не у
        `_probe_args_from_config` (он и есть про действующий runtime).

        Осознанные расхождения тени с runtime (тот же скоуп, что у основной секции):
        managed-слой `~/.gc/config.local.yml` и проектный `.gc/config.yml` в неё не
        входят. Сентинелы шаблона гасятся тем же правилом, что у пробы: свежая
        установка не должна показывать «your-backend:PORT» живым унаследованным
        адресом.

        Метод — ПРОВАЙДЕР (редактор зовёт его и на `_reload`), поэтому читает диск
        каждый раз: базовый файл правится и снаружи, пока открыт профиль."""
        from gc_core import config_raw
        from gc_core.config_modules.env_interpolation import (
            resolve_env_placeholders,
        )
        from gc_core.onboarding import configured_endpoint

        if not self._config_path:
            return {}
        document = config_raw.read_document(self._config_path)
        sections = resolve_env_placeholders(
            {
                key: document.get(key)
                for key in ("models", "slots", "assign", "backend")
            }
        )
        backend = sections.get("backend")
        backend = backend if isinstance(backend, dict) else {}
        url = backend.get("completions_url") or ""
        return {
            "models": sections.get("models") or {},
            "slots": sections.get("slots") or {},
            "assign": sections.get("assign") or {},
            "base_url": configured_endpoint(url),
        }

    def _open_profile_config(self, name: str) -> dict | None:
        """Конфиг стека «база + ОТКРЫТЫЙ профиль» — то, чем станет runtime, если этот
        профиль активировать. None — спрашивать нечего или незачем: открыт активный
        (runtime уже про него) либо стек не собрался (битый/исчезнувший файл), и тогда
        остаёмся на runtime, как было.

        Тот же принцип, которым рядом строится тень базы (`_base_registry_snapshot`):
        база окна собрана с УЖЕ АКТИВНЫМ профилем, и адрес с токеном, взятые
        оттуда, — из чужого подключения. Профиль со своим `backend` показывал бы
        «наследуется: <адрес активного>», Ctrl+Space и список моделей формы стучались
        бы туда же и чужим токеном, а при активации роутер увёл бы те же записи совсем
        на другой адрес — витрина и проба врали бы обе.

        Представление готовое: тем же `load_settings(..., profile_override=...)`
        приложение собирает базу при сессионном переключении профиля
        (`tui/app.py::_profile_remerge_settings`) — с той разницей, что там профиль
        применяют, а здесь показывают; побочек на процесс у сбора нет, так что
        показ ничего не меняет в работающем агенте. Стек не собрался — отвечают ДОКУМЕНТЫ
        (`_open_profile_connection_by_documents`), и в ответе тогда одно только
        подключение: чужой runtime витрине не подходит ничем."""
        if (
            not self._config_path
            or name == runtime_of(self._settings).active_profile
        ):
            return None
        from gc_core.config import load_settings

        try:
            # Витрина ПОКАЗЫВАЕТ чужой профиль, а не активирует его. Раньше для
            # этого был флаг `apply_to_process=False`: сбор перепривязывал
            # конвейер проекций всему процессу и глушил предупреждения TLS.
            # Обе побочки из сбора ушли, флаг снят.
            loaded = load_settings(self._config_path, profile_override=name)
        except Exception:
            return self._open_profile_connection_by_documents(name)
        return loaded

    def _open_profile_connection_by_documents(self, name: str) -> dict | None:
        """Подключение открытого профиля по ДОКУМЕНТАМ: `backend` базы, поверх
        которого лёг `backend` файла профиля (плюс `endpoint_presets` — их профиль
        переопределяет тем же слоем). Только эти ключи, зато без полной загрузки.

        Нужно потому, что собрать стек мешает ЛЮБАЯ его часть, а не подключение:
        чаще всего — фаза 2, ссылочная целостность слотов на merged-реестре
        («slots.default: моделей в реестре ≥2» у профиля со своими записями, но без
        своих слотов, над базой с реестром). К адресу и токену тот отказ отношения
        не имеет, а расплачивалась за него витрина: прежний `return None` уводил
        `_mount_open_profile` на runtime, и строка чужого файла показывала адрес
        АКТИВНОГО профиля, а Ctrl+Space стучался туда же его токеном — ровно та
        ложь, от которой предостерегает докстринг выше.

        Чужого профиля документный расчёт не видит ПО УСТРОЙСТВУ: читаются ровно
        два файла — база и этот профиль. Нечитаемый профиль — `{}` от
        `read_document`, то есть ответ одной базы; нечитаема и база — пусто, и это
        честнее чужого адреса.

        Скоуп тот же, что у тени базы (`_base_registry_snapshot`), с тем же
        осознанным расхождением: managed-слой `~/.gc/config.local.yml` и проектный
        `.gc/config.yml` в него не входят (быстрый путь их учитывает). Сентинелы
        шаблона здесь не гасятся: значения уходят в `_probe_args_from_config`, где
        это правило и живёт, — второй его реализации не заводим.

        Типы значений проверяются: сюда приходят как раз документы, схему НЕ
        прошедшие, и `completions_url: {}` из правленого руками YAML не должен
        доезжать до пробы."""
        from gc_core.config_modules.env_interpolation import (
            resolve_env_placeholders,
        )
        from gc_core.config_modules.merge import deep_merge

        row = self._profile_row_by_name(name)
        if row is None:
            return None
        merged = deep_merge(
            self._base_config_doc(), self._profile_document(row["ref"].path)
        )
        sections = resolve_env_placeholders(
            {key: merged.get(key) for key in ("backend", "endpoint_presets")}
        )
        backend = sections.get("backend")
        backend = backend if isinstance(backend, dict) else {}
        url, headers = (
            backend.get("completions_url"),
            backend.get("chat_completion_headers"),
        )
        proxy = backend.get("proxy")
        connection = {
            "completions_url": url if isinstance(url, str) else "",
            "chat_completion_headers": headers
            if isinstance(headers, dict)
            else {},
            "use_ssl": backend.get("use_ssl", True),
            "proxy": proxy if isinstance(proxy, str) else None,
        }
        # Пресеты — только годные по форме: их читают типизированно (строго),
        # а документ схему не проходил. Нет или не по форме — кодовый дефолт.
        presets = sections.get("endpoint_presets")
        if isinstance(presets, list):
            from gc_core.config_modules.endpoint_presets import EndpointPreset

            try:
                for item in presets:
                    EndpointPreset.model_validate(item)
            except ValueError:
                pass
            else:
                connection["endpoint_presets"] = presets
        return connection

    def _profile_document(self, path) -> dict:
        """Сырой документ профиля через кэш прохода — сосед `_profile_summary`:
        витрине под-вида нужны не только оверлеи, но и то, ЧТО из секций моделей
        лежит в файле (пустой реестр меняет состав форм, а с ним и состав
        «невидимого»)."""
        cache = self._profiles_disk_cache.setdefault("profile_docs", {})
        key = str(path)
        if key not in cache:
            from gc_core import config_raw

            cache[key] = config_raw.read_document(path)
        return cache[key]

    def _profile_form_sections(self, document: dict) -> set:
        """Секции файла, которые ПОКАЗЫВАЮТ формы редактора — только их превью и
        исключает. `models` показан всегда (список записей либо пустое состояние),
        а `slots`/`assign` — лишь пока непуст ЭФФЕКТИВНЫЙ реестр: тем же ПРЕДИКАТОМ
        (`effective_registry_nonempty` — общая функция редактора, не вторая его
        реализация), каким редактор решает, рисовать ли полный экран (§10-11 спеки
        эффективного вида). Профиль без своих `models` над основным конфигом С
        реестром строки слотов показывает — и называть их ещё и превью значило бы
        дублировать видимое; над легаси-базой строк нет ни у кого, и тогда превью —
        их единственный носитель (профиль, перенацеливающий слоты на алиасы нижнего
        слоя, законен и част).

        `level` не показывают формы НИ В ОДНОМ состоянии, поэтому в исключения он не
        входит: ручка уровня правит ГЛОБАЛЬНЫЙ `level:` конфига, а не ключ внутри
        файла профиля, и живёт в «Режимах работы» — без превью профиль с
        `level: max` не проявлялся бы на экране нигде (ruling координатора
        2026-08-11). В СТРОКЕ СПИСКА исключение остаётся — там канон §12.3 со своей
        колонкой состава (`profile_display_summary` не тронут)."""
        from gc_tui.editors.models_section import effective_registry_nonempty

        if effective_registry_nonempty(
            document, self._base_registry_snapshot()
        ):
            return {"models", "slots", "assign"}
        return {"models"}

    def _profile_overlay_names(self) -> list[str]:
        """Что профиль переопределяет ПОМИМО показанного формами: топ-уровневые ключи
        файла минус секции форм — механически и сырыми именами (§12.3).

        Пустые якоря не считаются (§11): писатель оставляет `slots: {}` после
        снятия последнего ключа, и витрина обязана читать это как отсутствие
        секции — иначе снятый слот навсегда оставлял бы строку «профиль также
        переопределяет: slots» о файле, который не переопределяет ничего."""
        row = self._profile_row_by_name(self._profiles_open)
        if row is None:
            return []
        document = self._profile_document(row["ref"].path)
        covered = self._profile_form_sections(document)
        return sorted(
            key
            for key, value in document.items()
            if key not in covered and not _empty_anchor(value)
        )

    def _profile_overlay_width(self) -> int:
        """Ширина строки превью внутри рамки (padding 0 1 — минус 2); до layout-прохода
        региона нет — 0, что трактуется как «без усечения» (carry-fix)."""
        widgets = list(self.query(".set-profiles-overlay"))
        if not widgets:
            return 0
        return max(0, widgets[-1].region.width - 2)

    def _profile_overlay_text(self) -> str:
        """Строка превью: усечение ЦЕЛЫМИ именами (`fit_mode_pairs` — тот же приём и
        тот же хвост «· …», что у превью «Режимов работы»): половина имени секции
        читалась бы как другое имя."""
        names = self._profile_overlay_names()
        width = self._profile_overlay_width()
        budget = (
            0
            if width <= 0
            else max(0, width - len(self._PROFILE_OVERLAY_HEAD))
        )
        return self._PROFILE_OVERLAY_HEAD + fit_mode_pairs(names, budget)

    def _profile_overlay_hint(self) -> str:
        """Подсказка под превью: список ЦЕЛИКОМ (усечённая строка выше могла его не
        вместить — та же пара «строка + hint», что у профиля в списке) плюс главный
        факт о них: форм для этих секций здесь нет, это YAML-территория (§12.4).

        Она же называет ход к ним (§11a): «правятся в YAML» без способа их увидеть
        оставляло бы человека вовсе без хода. Обещание гаснет вместе с самим
        просмотрщиком — теми же словами и по тому же гейту, что и в футере."""
        hint = (
            self._PROFILE_OVERLAY_HINT_HEAD
            + ", ".join(self._profile_overlay_names())
            + self._PROFILE_OVERLAY_HINT_TAIL
        )
        if not self._file_view_settings().enabled:
            return hint
        from gc_tui.editors.models_section import VIEW_KEY

        return f"{hint} · {VIEW_KEY}"

    def _refresh_profile_overlay(self) -> None:
        """Пересчитать превью оверлеев: и усечение — ПОСЛЕ layout-прохода, когда регион
        строки уже известен (carry-fix, как у `_refresh_menu_rows`), — и сам
        СОСТАВ. Состав меняет не только ширина: запись в профиль поднимает формы
        `slots`/`assign` (реестр стал непуст) или убирает их, а превью говорит ровно
        про то, чего формы НЕ показывают, — не пересчитанное, оно дублирует видимое
        или молчит о невидимом до перемонтирования под-вида.

        Переопределять нечего — обе строки гаснут: профиль из одних моделей молчит, а
        не показывает пустую фразу. Гаснут, а не сносятся: смонтированы они один раз
        (`_mount_open_profile`), и вернуть их на СВОЁ место посреди тела было бы
        нечем."""
        names = self._profile_overlay_names()
        for selector, render in (
            (".set-profiles-overlay", self._profile_overlay_text),
            (".set-profiles-overlay-hint", self._profile_overlay_hint),
        ):
            for widget in self.query(selector):
                widget.display = bool(names)
                widget.update(render() if names else "")

    _UPDATE_ACTION_UPDATE = "Обновить агента"
    _UPDATE_ACTION_CHECK = "Проверить обновления"
    _UPDATE_ACTION_SYNC = "Обновить config.yml до текущей версии"
    _UPDATE_ACTION_RESET = "Сбросить конфиг к умолчанию"

    def _update_confirm_text(self) -> str:
        """I4: «в отдельном окне» правда только на Windows — на остальных платформах
        установка идёт в фоне без видимого окна (см. updater.runner.install_location_phrase).

        Если идёт ход агента — предупреждаем явно: закрытие прервёт выполнение (диалог
        сохранится в тред, но текущий ход не доведётся)."""
        from gc_core.updater.runner import install_location_phrase

        runner = getattr(self.app, "active_runner", None)
        running = runner is not None and runner.is_running()
        head = (
            "Идёт выполнение задачи — обновление прервёт его и закроет агента"
            if running
            else "Агент закроется"
        )
        return (
            f"{head}, установка пойдёт {install_location_phrase()}; когда "
            "завершится — запустите gc-ua заново. Текущий диалог сохранится "
            "в тред. Enter — обновить · Esc — отмена"
        )

    def _mount_update_section(self, frame: Vertical) -> None:
        from gc_core import app_version
        from gc_core.updater.source import resolved_source

        src = app_version.install_source()
        dev = app_version.is_dev_install()  # признак один на все экраны
        # DI через _source: app_version.install_source привязан late-binding'ом к
        # моменту импорта updater.source — в тестах monkeypatch этого не увидит.
        resolved = None if dev else resolved_source(_source=lambda: src)
        ver = Text()
        ver.append("Версия     ", style=DEEP_DIM)
        if dev:
            ver.append(app_version.DEV_LABEL, style=DIM)
        else:
            ver.append(f"v{app_version.agent_version()}", style=DIM)
            ver.append(f" · {src.ref or 'master'}", style=DIM)
        # set-info: отступ как у #set-title/.set-row; set-gap: зазор под заголовком.
        frame.mount(Static(ver, classes="set-info set-dyn set-gap"))
        if not dev:
            line = Text()
            line.append("Источник   ", style=DEEP_DIM)
            line.append(src.url, style=DIM)
            frame.mount(Static(line, classes="set-info set-dyn"))
            frame.mount(
                Static(self._checked_line_text(), classes="set-info set-dyn")
            )
        actions = (
            [self._UPDATE_ACTION_SYNC, self._UPDATE_ACTION_RESET]
            if dev
            else [
                self._UPDATE_ACTION_UPDATE,
                self._UPDATE_ACTION_CHECK,
                self._UPDATE_ACTION_SYNC,
                self._UPDATE_ACTION_RESET,
            ]
        )
        self._update_actions = actions
        if self._update_idx is None or self._update_idx >= len(actions):
            self._update_idx = (
                0  # дефолт — верхнее действие (главный смысл секции)
            )
        update_available = (
            self._update_check_result is not None
            and self._update_check_result.status == "update"
        )
        for i, label in enumerate(actions):
            selected = i == self._update_idx
            row = Text()
            row.append("▸ " if selected else "  ", style=ACCENT)
            # доступное обновление — хорошая новость → accent (teal), не алярм-warn
            row.append(
                label, style=ACCENT if (i == 0 and update_available) else INK
            )
            # первая строка-действие — один класс с top+bottom margin (раздельные
            # .set-gap+.set-spaced Textual перетирает → пропадал воздух под первым действием)
            spacing = "set-spaced-gap" if i == 0 else "set-spaced"
            cls = f"set-row set-dyn {spacing}" + (
                _CLS_SELECTED if selected else ""
            )
            frame.mount(Static(row, classes=cls))
        if self._update_awaiting_confirm:
            frame.mount(
                Static(
                    Text(self._update_confirm_text(), style=WARN),
                    classes=f"set-confirm set-dyn {KEEP_IN_VIEW}",
                )
            )
        elif self._reset_awaiting_confirm:
            frame.mount(
                Static(
                    Text(self._reset_confirm_text(), style=WARN),
                    classes=f"set-confirm set-dyn {KEEP_IN_VIEW}",
                )
            )
        else:
            # Высота — под самую длинную подсказку действий (дизайн-код §9).
            frame.mount(
                ReservedHint(
                    self._update_hint_text(
                        actions[self._update_idx], resolved
                    ),
                    variants=lambda: [
                        self._update_hint_text(a, resolved) for a in actions
                    ],
                    classes="set-hint set-dyn set-update-hint",
                )
            )

    def _checked_line_text(self) -> Text:
        """Строка «Проверено» секции «Обновление» (Task 7): исходное состояние /
        «проверяю…» в полёте воркера / вердикт последней проверки."""
        t = Text()
        t.append("Проверено  ", style=DEEP_DIM)
        if self._update_checking:
            t.append("проверяю…", style=DEEP_DIM)
            return t
        r = self._update_check_result
        if r is None:
            t.append("— не проверялось", style=DIM)
            return t
        if r.status == "update":
            t.append(f"↑ {r.detail}", style=ACCENT)
        elif r.status == "current":
            t.append(f"✓ {r.detail}", style=OK)
        else:
            t.append(f"— не удалось: {r.detail}", style=DIM)
        return t

    def _update_hint_text(self, action: str, resolved) -> str:
        """Контекстная подсказка под списком действий (§5, тексты verbatim)."""
        if action == self._UPDATE_ACTION_UPDATE:
            from gc_core.updater.check import extra_args
            from gc_core.updater.runner import (
                build_install_command,
                install_location_phrase,
            )

            # [0] — абсолютный путь к uv (нужен detached-скрипту); в подсказке для юзера
            # показываем короткое «uv», как он сам набрал бы в терминале.
            cmd = " ".join(
                ["uv"]
                + build_install_command(resolved, extra_args(self._settings))[
                    1:
                ]
            )
            return (
                f"Закроет агента, установка пойдёт {install_location_phrase()}; когда завершится — "
                "запустите gc-ua заново. MCP и config.yml предложит обновить при "
                "следующем запуске.\n"
                f"То же из терминала: {cmd}"
            )
        if action == self._UPDATE_ACTION_CHECK:
            return (
                f"Спросит у источника установки ({resolved.url} · {resolved.ref or 'master'}), "
                "появились ли новые версии. Ничего не устанавливает."
            )
        if action == self._UPDATE_ACTION_SYNC:
            return (
                "Перепишет файл по образцу текущей версии, ваши значения сохранятся, "
                "старый файл — в бэкап. На работу агента не влияет."
            )
        return (
            "Заменит config.yml пакетным умолчанием текущей версии — ваши правки уйдут "
            "в бэкап. Файлы профилей и настройки сессии останутся, но выбор профиля "
            "в config.yml сбросится."
        )

    def _run_update_action(self) -> None:
        if not self._update_actions:
            return
        action = self._update_actions[self._update_idx]
        if action == self._UPDATE_ACTION_SYNC:
            self._run_sync()
        elif action == self._UPDATE_ACTION_CHECK:
            self._check_updates_now()
        elif action == self._UPDATE_ACTION_UPDATE:
            self._request_update()
        elif action == self._UPDATE_ACTION_RESET:
            self._request_reset()

    def _check_updates_now(self) -> None:
        """Enter на «Проверить обновления»: фоновый воркер check_for_update, результат —
        в строку «Проверено» (§5); при найденном обновлении курсор переходит на
        «Обновить агента» (пересборка действий, warn)."""
        from gc_core import app_version
        from gc_core.updater.source import resolved_source

        src = app_version.install_source()
        if src is None or src.editable:
            return
        resolved = resolved_source(_source=lambda: src)
        if resolved is None:
            return
        self._update_checking = True
        self._render_view()

        def _work() -> None:
            from gc_core.updater import check as check_mod

            try:
                result = check_mod.check_for_update(resolved)
            except Exception as exc:
                result = check_mod.CheckResult(
                    "unknown", None, f"не удалось: {exc}"
                )
            self.app.call_from_thread(self._apply_check_result, result)

        self.run_worker(_work, thread=True, name="settings-update-check")

    def _apply_check_result(self, r) -> None:
        """Воркер `check_for_update` прилетает на UI-поток (I1: НЕ гейтить персист текущим
        `_view` — юзер мог уйти из секции 4 пока проверка шла; иначе `_update_checking`
        навсегда `True` и секция залипает на «проверяю…»). Персист-вердикт (I2) пишем
        через тот же `record_check_result`, что и стартовый авто-чек (`app.py`) — источник
        правды у карточки/`/notices` и у ручной проверки один."""
        self._update_checking = False
        self._update_check_result = r
        self.app._update_check_result = (
            r  # вердикт сессии — общий (хаб, повторный вход в секцию)
        )
        if r.status == "update":
            self._update_idx = 0  # «Обновить агента» первым, см. §5
        import time

        from gc_core import app_version, upgrade_gate
        from gc_core.updater.check import (
            record_check_result,
            update_available_from_state,
        )

        src = app_version.install_source()
        installed = src.commit if src else None
        record_check_result(time.time(), r.remote_commit, installed)
        self.app._update_available = update_available_from_state(
            upgrade_gate.read_state(), installed
        )
        main = self.app._main_screen()
        if main is not None and hasattr(main, "refresh_update_notice"):
            main.refresh_update_notice()
        if not self.is_mounted or self._view != _UPDATE:
            return
        self._render_view()

    def _request_update(self) -> None:
        """Enter на «Обновить агента»: показать confirm-баннер (§5).
        При актуальной версии — только title-note, окно не открываем (Task 7)."""
        r = self._update_check_result
        if r is not None and r.status == "current":
            self._title_note("уже установлена последняя версия")
            return
        if self._update_awaiting_confirm:
            return
        self._update_awaiting_confirm = True
        self._render_view()

    def _confirm_update(self) -> None:
        if not self._update_awaiting_confirm:
            return
        import os

        from gc_core import app_version
        from gc_core.updater.check import extra_args
        from gc_core.updater.runner import (
            build_install_command,
            install_location_phrase,
            spawn_detached_update,
        )
        from gc_core.updater.source import resolved_source

        src = app_version.install_source()
        resolved = resolved_source(_source=lambda: src)
        if resolved is None:
            self._update_awaiting_confirm = False
            self._render_view()
            return
        command = build_install_command(resolved, extra_args(self._settings))
        # Без авто-рестарта — единообразно с `gc-cli --update`: фоновая переустановка,
        # пользователь запускает gc-ua заново (venv нельзя заменить под живым процессом).
        spawn_detached_update(
            command, parent_pid=os.getpid(), restart_cmd=None
        )
        self.app.exit(
            message=f"Установка запущена {install_location_phrase()}. Когда завершится — запустите gc-ua заново."
        )

    def _cancel_update(self) -> None:
        self._update_awaiting_confirm = False
        self._render_view()

    def _run_sync(self) -> None:
        from gc_core import config_sync

        if not self._writable():
            self._notice("✗ пакетный шаблон не обновляется")
            return
        from pathlib import Path

        from gc_core.config import _default_config_path

        try:
            user_text = Path(self._config_path).read_text(encoding="utf-8")
            template_text = Path(_default_config_path()).read_text(
                encoding="utf-8"
            )
            if not config_sync.needs_sync(user_text, template_text):
                self._notice("config.yml уже актуален — обновлять нечего")
                return
        except (OSError, ValueError):
            pass  # не смогли решить — пусть sync решит
        try:
            report = config_sync.sync_config(self._config_path)
        except (ValueError, OSError) as exc:
            self._notice(f"✗ config.yml НЕ обновлён: {exc}")
            return
        self._raw = self._read_raw()  # файл переписан по шаблону — перечитать
        lines = []
        try:
            self._on_apply()
            self._pull_base_from_app()
        except Exception as exc:
            lines.append(f"сохранено, но конфиг не перечитан: {exc}")
        lines.append(
            f"✓ config.yml обновлён · перенесено: {report.transferred} · переименовано: {report.renamed}"
        )
        if report.unknown:
            lines.append(
                f"неизвестных ключей: {', '.join(report.unknown)} (в бэкапе)"
            )
        if report.reenter:
            lines.append(f"задайте заново: {', '.join(report.reenter)}")
        lines.append(f"бэкап: {report.backup_path}")
        self._report("\n".join(lines))

    def _reset_confirm_text(self) -> str:
        return (
            "config.yml будет заменён пакетным умолчанием текущей версии, ваш файл — "
            "в бэкап. Файлы профилей и настройки сессии останутся, но выбор профиля "
            "в config.yml сбросится. Enter — сбросить · Esc — отмена"
        )

    def _request_reset(self) -> None:
        """Enter на «Сбросить конфиг к умолчанию»: confirm-баннер (канон §5)."""
        if self._reset_awaiting_confirm or self._refuse_profile_switch():
            return
        self._reset_awaiting_confirm = True
        self._render_view()

    def _cancel_reset(self) -> None:
        self._reset_awaiting_confirm = False
        self._render_view()

    def _confirm_reset(self) -> None:
        if not self._reset_awaiting_confirm:
            return
        self._reset_awaiting_confirm = False
        if self._refuse_profile_switch(rerender=True):
            return
        if not self._writable():
            self._render_view()
            self._notice("✗ пакетный шаблон не сбрасывается")
            return
        from gc_core import config_sync

        try:
            report = config_sync.reset_config(self._config_path)
        except (ValueError, OSError) as exc:
            self._render_view()
            self._notice(f"✗ config.yml НЕ сброшен: {exc}")
            return
        self._raw = self._read_raw()  # файл заменён шаблоном — перечитать
        self._origins = self._read_origins()
        lines = [
            f"✓ config.yml сброшен к умолчанию · бэкап: {report.backup_path}"
        ]
        try:
            self._on_apply()
            self._pull_base_from_app()
        except Exception as exc:
            lines.append(f"сброшено, но конфиг не перечитан: {exc}")
        session_overrides = getattr(self.app, "_session_overrides", None) or {}
        session_profile = session_overrides.get("profile")
        if session_profile:
            lines.append(
                f'сессия остаётся на профиле "{session_profile}" (/profile) — сброс базы это не меняет'
            )
        from gc_core import onboarding

        if onboarding.is_unconfigured(self._settings):
            lines.append(
                "настройка не завершена — откройте помощник (--init-config) или впишите поля вручную"
            )
        self._render_view()
        self._report("\n".join(lines))

    # --- MCP-секция (спека C) ---
    def _mcp_editor(self):
        from gc_tui.editors.mcp_section import McpSectionEditor

        try:
            return self.query_one(McpSectionEditor)
        except NoMatches:
            return None

    def _mcp_changed(self) -> None:
        runner = getattr(self.app, "active_runner", None)
        if runner is not None and runner.is_running():
            self._notice("MCP применятся со следующего запуска")

    def _mcp_result_notice(self, results: list) -> None:
        if results:
            self._notice(
                ", ".join(f"{n} {'✓' if ok else '✗'}" for n, ok in results)
            )

    # --- apply ---
    def _singleton(self, wid: str, text, cls: str) -> None:
        """Обновить существующий Static #wid либо смонтировать новый. remove() в
        Textual асинхронен → remove+mount в одном такте роняет DuplicateIds;
        update идемпотентен и не зависит от повторных нажатий.

        Виджет, УЖЕ помеченный на удаление (`_pruning`), существующим не считаем: он
        ещё в дереве, но обречён — обновление исчезло бы вместе с ним. Так терялась
        плашка результата, когда предыдущую в том же такте снесла преамбула `on_key`
        (гасит `#set-flash` на любой клавише) или `_render_view`: снести они успевают,
        применить удаление — нет.

        Замена монтируется в СВОЮ обёртку: id уникален в пределах родителя, а обречённый
        держит свой до применения удаления — второй Static с тем же id рядом с ним упал
        бы DuplicateIds. Ждать освобождения id нельзя: отложенный повтор крутится в
        собственной очереди сообщений и до применения удаления не доходит (livelock).
        Обёртка занимает ноль строк (height: auto) и уезжает вместе с содержимым фрейма
        при следующем `_render_view`.

        `markup=False`: сюда приезжают ЧУЖИЕ тексты — сообщения валидаторов и
        исключений. Квадратные скобки в них (`[type=extra_forbidden,
        input_value=[], input_type=list]` от pydantic) — данные, а не разметка, и
        разбор их роняет приложение `MarkupError`. Единственный вызов с готовым
        `Text` (`set-envconfirm`) флаг не задевает — у `Text` разметки нет вовсе."""
        # Плашка приезжает БЕЗ пересборки тела (значит, без `reveal_focus` из
        # `_render_view`), а тело длиннее вьюпорта: и вопрос о перезаписи ${ENV:VAR},
        # и итог записи внизу секции оказывались за краем. Подводим РАЗОВО, приоритета
        # вопроса не выдаём: `#set-notice` живёт до пересборки тела и, получив ярус
        # `KEEP_IN_VIEW`, навсегда перебивал бы курсор. Кто из зовущих показывает
        # вопрос, а не сообщение, тот сам кладёт маркер в `cls` (см. `set-envconfirm`).
        for w in self.query(f"#{wid}"):
            if w._pruning:
                continue
            w.update(text)
            w.set_classes(cls)
            reveal(w)
            return
        slot = Vertical(
            Static(text, id=wid, classes=cls, markup=False),
            classes="set-dyn set-slot",
        )
        self.query_one(_SEL_SET_FRAME, Vertical).mount(slot)
        reveal(slot)

    def _notice(self, text: str) -> None:
        # Реплика ЗАМЕЩАЕТ отчёт: два сообщения об одном экране разом не живут, а
        # оставленный флаг заставлял бы футер обещать закрытие того, чего нет
        # (находка ревью хвоста).
        self._dismiss_report()
        self._singleton("set-notice", text, f"set-hint set-dyn {EPHEMERAL}")

    def _report(self, text: str) -> None:
        """ОТЧЁТ операции (актуализация/сброс config.yml): его читают и по нему
        действуют — гаснуть на следующей клавише он не имеет права (ревизия
        владельца по смоку). Живёт до Esc (своя ступень лестницы) либо до смены
        секции; перерисовку СВОЕЙ секции переживает — текст хранится в состоянии
        экрана и перемонтируется в `_render_view`. Оформление — своя плашка
        `set-report`: фон отделяет материал для чтения от реплик, а бордер
        успеха, не янтарный язык подтверждений — отчёт не тревога.

        Свой id, не общий с репликами: `_notice`/`_flash` иначе перетирали бы
        текст отчёта в том же слоте, оставляя флаг — футер обещал бы закрытие
        того, чего на экране уже нет (находка ревью хвоста)."""
        self._report_text = text
        self._report_section = self._view
        self._mount_report(text)
        self._refresh_footer()  # отчёт держит экран — футер обещает его закрытие

    def _mount_report(self, text: str) -> None:
        self._singleton(
            "set-report",
            f"{text}\n\n{_REPORT_DISMISS_HINT}",
            f"set-report set-dyn {KEEP_UNTIL_DISMISSED}",
        )

    def _collect_values(self, section: int) -> dict | None:
        """Значения секции; None = ошибка валидации редактора (уже показана);
        {} = поле не менялось (писать нечего).

        skip-unchanged сверяется с ФАЙЛОМ (_is_dirty), а не с резолвнутым конфигом:
        иначе поле со ссылкой ${ENV:VAR} всегда «отличается» от конфига (там уже
        подставленное значение) и каждое сохранение выписывает секрет в файл литералом.
        Заодно нетронутое поле не проходит через submit() — ссылка не спотыкается
        о валидацию значения, которым её никто не менял."""
        if not self._is_dirty(section):
            return {}
        values: dict = {}
        if section == _CONNECTION:
            url, header, error, proxy = self._connection_editor().submit()
            if error is not None:
                self._title_note(
                    error
                )  # блок: «введите адрес» / «нужны http(s)://…»
                return None
            if url is not None:
                values["completions_url"] = url
            if header is not None:
                values["Authorization"] = header
            if proxy is not None:
                values["proxy"] = proxy
        elif section == _MODELS:
            value = self._legacy_model_editor().submit()
            if value is not None:
                values["model"] = value
        return values

    def _flash(self, text: str, *, outcome: str = "ok") -> None:
        """Заметная плашка результата (по языку confirm-баннера): фон + левый бордер +
        ведущий глиф, окраска по исходу. outcome: "ok" (зелёная ✓) | "warn" (янтарная ⚠)."""
        glyph = "✓ " if outcome == "ok" else "⚠ "
        cls = (
            "set-dyn set-flash-ok"
            if outcome == "ok"
            else "set-dyn set-flash-warn"
        )
        self._dismiss_report()  # плашка замещает отчёт — см. `_notice`
        self._singleton("set-flash", glyph + text, f"{cls} {EPHEMERAL}")

    def _env_refs_overwritten(
        self, values: dict
    ) -> tuple[list[str], list[str]]:
        """Переменные окружения, чьи ссылки в файле затрёт сохранение, разложенные
        по тому, ЧТО с ними произойдёт: (перезапись литералом, удаление пустым
        значением). Оба списка пусты — затирать нечего либо новое значение само ссылка.

        Раскладка нужна предупреждению: у литерала последствие — утечка секрета в
        файл, у пустого поля — исчезновение ссылки. Одним словом их не назвать.

        Правило — общее (`config_raw.env_refs_overwritten`): тем же предикатом
        судит себя форма записи реестра (`models_section._submit_form`), и вторая
        редакция здесь снова разъехалась бы с ней (L22)."""
        from gc_core import config_raw

        return config_raw.env_refs_overwritten(values, self._raw)

    def _env_confirm_text(self, written: list[str], erased: list[str]) -> str:
        """Предупреждение называет ПОСЛЕДСТВИЕ, а не сам факт затирания.

        Единый текст «запишет введённое значение открытым текстом» пугал утечкой и
        на пути `Ctrl+U`, где поле пустое и ничего не пишется — ссылка удаляется.
        Оба списка разом реальны: в «Подключении» ссылками могут быть и адрес, и
        токен, и судьба у них может разойтись."""
        parts = []
        if written:
            parts.append(
                f"{', '.join(written)} — сохранение запишет введённое значение в файл открытым текстом"
            )
        if erased:
            parts.append(
                f"{', '.join(erased)} — сохранение удалит ссылку из файла"
            )
        return (
            "В файле — ссылка на переменную окружения: "
            + ";  ".join(parts)
            + ".  Enter — сохранить  ·  Esc — отмена"
        )

    def _cancel_env_confirm(self) -> None:
        """Снять confirm перезаписи ссылки. Секцию НЕ перерисовываем: ре-рендер поднял бы
        редактор заново из файла и потерял введённое значение."""
        self._pending_env_save = None
        for w_ in self.query("#set-envconfirm"):
            w_.remove()

    def _do_save(
        self,
        section: int,
        values: dict,
        *,
        warn_detail: str = "",
        confirmed: bool = False,
        back_to_menu: bool = True,
    ) -> None:
        """Записать плоские поля секции и отчитаться (контракт §3: возврат в хаб + флэш).

        `back_to_menu=False` — остаться в секции: под-форма «резервной модели» открыта
        ИЗ списка записей, и уводить с неё в меню значит подниматься на два уровня там,
        где Esc поднимает на один. Тело секции всё равно пересобирается тем же
        `_render_view`, поэтому список сам показывает записанное."""
        from gc_core import onboarding_config_writer as w
        from gc_core.config import _default_config_path, validate_config_text
        from gc_core.onboarding import FIELD_ANCHOR_PATHS

        if not confirmed:
            # Единственная точка записи → ссылка на переменную не может стать литералом
            # молча, каким бы путём (probe, force, секция 1) сюда ни пришли.
            written, erased = self._env_refs_overwritten(values)
            if written or erased:
                self._pending_env_save = (
                    section,
                    values,
                    warn_detail,
                    back_to_menu,
                )
                self._singleton(
                    "set-envconfirm",
                    Text(self._env_confirm_text(written, erased), style=WARN),
                    f"set-confirm set-dyn {KEEP_IN_VIEW}",
                )
                return
        dotted = {FIELD_ANCHOR_PATHS.get(k, k): v for k, v in values.items()}
        try:
            missing = w.write_values(
                self._config_path,
                dotted,
                template_path=_default_config_path(),
                validate=validate_config_text,
            )
        except (ValueError, OSError) as exc:
            self._notice(f"✗ не сохранено: {exc}")
            return
        if missing:
            # Файл не тронут (запись «всё или ничего»): рапортовать успех, оставив
            # человека со старым конфигом, нельзя (находка ревью коллеги).
            self._notice(
                f"✗ не сохранено: не нашлись ключи {', '.join(missing)}"
            )
            return
        self._raw = (
            self._read_raw()
        )  # файл изменился — сырые значения перечитать
        # Состояние раннера снимаем ДО _on_apply: reload_config обнуляет active_runner,
        # а счётчики токенов/стоимости сессии живут в executor'е — после пересборки
        # они начинаются с нуля. Молча терять их нельзя: юзер видит цифры в футере.
        runner = getattr(self.app, "active_runner", None)
        was_running = runner is not None and runner.is_running()
        had_session = runner is not None
        try:
            self._on_apply()
            self._pull_base_from_app()
        except Exception as exc:
            self._notice(f"сохранено, но конфиг не перечитан: {exc}")
            return
        # v2: возврат в хаб + флэш (контракт §3); повторный config-probe (секции 0/1)
        if back_to_menu:
            self._view = "menu"
        self._render_view()
        # Перечитать происхождение: файл изменился, оверлеи могли начать/перестать перекрывать.
        self._origins = self._read_origins()
        overridden = [
            k
            for k in values
            if self._origin(k) is not None and self._origin(k).is_overridden
        ]
        # Побочные последствия сохранения — общие для обеих веток: перекрытие поля не
        # отменяет ни пересборку конфига (счётчики уже обнулены), ни непройденную probe.
        extras = []
        if not was_running and had_session:
            extras.append("счётчики токенов сессии обнулены")
        if warn_detail:
            extras.append(warn_detail)
        if overridden:
            from gc_core.config_origin import overlay_label

            origin = self._origin(overridden[0])
            source = overlay_label(origin)
            # «применится со следующего запуска» здесь НЕ говорим — не применится вовсе.
            # Поле называем по имени: флеш виден уже на хабе, где секции не видно.
            lines = [
                "✓ записано в config.yml — но сейчас не действует:",
                f"  поле «{SECTIONS[section]}» перекрыто {source}",
            ]
            lines.extend(f"  {extra}" for extra in extras)
            self._notice("\n".join(lines))
            if section in _PROBE_SECTIONS:
                self._start_config_probe()
            return
        parts = ["Сохранено"]
        if was_running:
            parts = ["Сохранено — применится со следующего запуска."]
        elif had_session:
            parts.append("Счётчики токенов сессии обнулены.")
        outcome = "ok"
        if warn_detail:
            parts.append(warn_detail)
            outcome = "warn"
        # «Сохранено» без точки, когда это всё; точка — только разделитель перед продолжением
        text = (
            parts[0]
            if len(parts) == 1
            else parts[0].rstrip(".") + ". " + " ".join(parts[1:])
        )
        self._flash(text, outcome=outcome)
        if section in _PROBE_SECTIONS:
            self._start_config_probe()

    # --- навигация ---
    def _move_menu(self, delta: int) -> None:
        self._menu_idx = (self._menu_idx + delta) % len(SECTIONS)
        self._refresh_menu_rows()
        # Меню обновляет строки НА МЕСТЕ, без пересборки тела, — значит и без
        # `reveal_focus`, который зовёт `_render_view`: на низком окне курсор уезжал
        # под нижнюю границу рамки. Подводка нужна там же, где двигается курсор.
        reveal_focus(self)

    def _sync_config_from_app(self) -> None:
        """Подтянуть базу экрана из app при входе в секцию (см. _open_section):
        сама явно зовёт гейт свежести config.yml (reload_config_if_changed) —
        другое окно могло сохранить умолчание, пока этот экран уже открыт — и
        только потом берёт актуальную базу через base_settings() (тот её лишь
        отдаёт, без собственной автоперечитки).

        Совпадение по ссылке — файл не менялся с прошлой синхронизации
        (reload_config всегда собирает новый объект, см. TuiApp.reload_config),
        сырые значения/происхождение перечитывать незачем: тот же приём хвоста
        записи, что и в _do_save/_modes_apply, только без своего
        reload — здесь его уже выполнил гейт."""
        self.app.reload_config_if_changed()  # вход в секцию: подхватить чужую правку глобала (дизайн 2026-07-21 §4)
        fresh = self.app.base_settings()
        if fresh is self._settings:
            return
        self._settings = fresh
        self._raw = self._read_raw()
        self._origins = self._read_origins()

    def _open_section(self, i: int) -> None:
        self._sync_config_from_app()
        self._view = i
        if i == _VOICE:
            self._kick_voice_alive()  # свежая живость к первому кадру секции
        self._reset_draft_state()  # бампает gen — стухший воркер прежней секции отбрасывается
        self._cancel_autoprobe()  # осиротевший таймер прошлого захода в «Подключение» не копим
        self._cancel_voice_confirm()  # уход из секции отменяет незавершённый confirm
        self._reset_voice_probe()  # + вердикт/воркер probe прошлого сеанса
        self._voice_cloud_draft = (
            None  # черновик endpoint живёт в пределах одного захода
        )
        self._voice_list_reopen_gate = (
            False  # гейт одноразовый и в пределах одного захода
        )
        # Курсор голосовой секции — на дефолт при КАЖДОМ заходе, как `_update_idx` ниже и
        # как все прочие секции. Память места между заходами была исключением одной этой
        # секции (живой смок 2026-08-20): возврат из меню попадал сразу в поле «Модель»
        # или «Язык», и список раскрывался там, где человек его не просил.
        self._voice_cursor_placed = False
        self._voice_group = _VOICE_GROUP_VARIANTS
        self._voice_field_idx = 0
        self._modes_idx = 0  # то же правило: «Режимы» тоже помнили место
        # Под-состояния «Профилей» живут ровно один заход в секцию (тем же приёмом,
        # что курсор и черновики остальных): вернувшийся сюда человек не должен
        # застать чужой ввод имени, неотвеченный вопрос об удалении или чужой
        # открытый профиль. Через этот же метод идут PgUp/PgDn (`_switch_section`),
        # поэтому смена секции снимает их даром.
        self._profiles_name_prompt = None
        self._profiles_delete_name = None
        self._profiles_open = None
        if i == _PROFILES:
            self._profiles_idx = self._profiles_active_index()
        if i == _UPDATE:
            self._update_idx = (
                None  # пересчитать дефолт-курсор при (пере)входе в секцию
            )
        self._render_view()
        # Авто-проверка при ПЕРВОМ входе в «Обновление» за сессию (как probe в др. секциях):
        # секция сразу показывает статус, а не «не проверялось». Кэш (_update_check_result)
        # переживает повторные входы → сеть не долбим; «Проверить обновления» форсит свежий.
        # На dev-установке _check_updates_now — no-op (источника обновления нет).
        if (
            i == _UPDATE
            and self._update_check_result is None
            and not self._update_checking
        ):
            self._check_updates_now()
        # Авто-проверка при входе в «Подключение»: сразу полный differential-вердикт
        # «сервер+токен» (как ручная проверка), а не нейтральный config-probe. Таймер, НЕ
        # call_after_refresh: рекурсивный refresh-callback голодом морит message pump
        # (дети редактора не успевают смонтироваться → вечный цикл). Уход из секции/
        # экрана стухает таймер через gen (_reset_draft_state бампает _draft_gen) — но
        # если человек жмёт Enter/Ctrl+Space ДО срабатывания таймера, обе пробы делят
        # _draft_gen, и автопроба, стартовавшая следом, обесценивает результат
        # пользовательской (а у неё в on_done — сохранение). Поэтому храним хэндл и
        # снимаем его в Enter/Ctrl+Space непосредственно перед стартом СВОЕЙ пробы —
        # не раньше: ранняя безусловная отмена (напр. когда Enter поглощён
        # пресет-листом адреса и своя проба не стартует) гасила бы автопробу без
        # замены — связь осталась бы непроверенной. См. _cancel_autoprobe
        # (гонка унаследована из develop 444bb20b).
        if i == _CONNECTION:
            self._autoprobe_timer = self.set_timer(
                0.05, self._autorun_connection_probe
            )

    def _cancel_autoprobe(self) -> None:
        """Снять запланированную автопробу: человек сам запустил проверку.

        Обе пробы делят _draft_gen, и автопроба, стартовавшая ПОСЛЕ пользовательской,
        обесценивает её результат — а у пользовательской в on_done висит сохранение.
        Отсюда «нажал Enter, а не сохранилось» (гонка develop 444bb20b).

        Звать непосредственно перед СВОИМ вызовом _start_draft_probe (не раньше,
        не «на всякий случай» в начале обработчика) — иначе можно погасить
        автопробу на пути, где своя проба не стартует (ветка вернулась раньше),
        и вердикт связи не появится вообще, без замены."""
        timer = self._autoprobe_timer
        self._autoprobe_timer = None
        if timer is not None:
            timer.stop()

    def _autoprobe_on_leave(self, ed, leaving_id) -> None:
        """Уход с поля «Подключения» — триггер автопробы (§5.2, как в мастере).

        Звать ПОСЛЕДНИМ в обработчике стрелки — после всего, что может бампнуть
        `_draft_gen`: проба, стартовавшая раньше такого бампа, обесценила бы сама
        себя же. В «Подключении» после этой правки поколение на стрелке не
        бампает никто (см. обработчик), но правило — про порядок, а не про
        сегодняшний набор вызовов.

        Снимок полей — против дребезга: ходьба по форме без правок не должна
        долбить сервер на каждой стрелке. Пишет его сам `_start_draft_probe`, а
        обнуляет `_reset_draft_state` (снимок мёртв вместе с обесцененной пробой),
        поэтому автопроба входа в секцию и ручные пробы тоже считаются. Обратная
        сторона снимка — на неизменных полях повтор невозможен по построению
        («сервер лежал, поля не трогали»), для этого случая и заведён ручной
        `Ctrl+Space`: он идёт мимо снимка (§5.2).

        Нового окна гонки с пользовательской пробой здесь не появляется: старт
        синхронный, прямо в обработчике клавиши, — позже нажатого `Enter` эта
        проба стартовать не может. А запланированный таймер входа в секцию
        гасим непосредственно перед стартом СВОЕЙ пробы — то же правило, что у
        `Enter`/`Ctrl+Space` (см. `_cancel_autoprobe`): иначе он выстрелит следом
        и обесценит уже этот, более свежий, вердикт."""
        if leaving_id not in ("conn-url", "conn-token", "conn-proxy"):
            return  # курсор шёл не с поля (галка «без авторизации») — уходить неоткуда
        if leaving_id == "conn-url" and ed.presets_open:
            return  # стрелка водила по пресет-листу, поле адреса не покидали
        url = ed.peek()[0]
        if ed.probe_snapshot() == self._last_probed_snapshot:
            return  # ровно те же значения уже проверены (или проверяются)
        if not url and not self._probe_args_from_config()[0]:
            # Адреса нет ни в поле, ни в конфиге: _start_draft_probe вышел бы до
            # старта — гасить таймер входа в секцию на этом пути значит снять
            # автопробу БЕЗ замены (запрет из докстринга _cancel_autoprobe).
            return
        self._cancel_autoprobe()
        self._start_draft_probe(_CONNECTION)

    def _autorun_connection_probe(self) -> None:
        # Timer.stop() — best-effort, не гарантия (на Windows таймер ждёт в отдельном
        # потоке; если дедлайн уже наступил, отмена может опоздать). Поэтому решает
        # СВОЙ хэндл: снят в _cancel_autoprobe → значит опоздали, выходим молча.
        if self._autoprobe_timer is None:
            return
        self._autoprobe_timer = None  # таймер уже отработал
        if self._view != _CONNECTION or not self.is_mounted:
            return
        try:
            editor = self._connection_editor()
        except NoMatches:
            return  # read-only конфиг — редактор не смонтирован, probe не нужен
        if not editor.query("#conn-url"):
            # дети редактора ещё компонуются — peek() упал бы NoMatches; повторим чуть позже
            self._autoprobe_timer = self.set_timer(
                0.05, self._autorun_connection_probe
            )
            return
        self._start_draft_probe(_CONNECTION)

    def _switch_section(self, delta: int) -> None:
        if self._view == "menu":
            return
        new = self._view + delta
        if 0 <= new < len(SECTIONS):  # края без wrap (контракт §3)
            self._menu_idx = new
            self._open_section(new)

    def _ask_dirty_exit(self) -> None:
        self._dirty_exit_confirm = True
        self._dirty_exit_focus = self.focused
        self.set_focus(None)
        self._singleton(
            "set-dirty-confirm",
            Text(
                "Выйти без сохранения? Enter — выйти без сохранения · Esc — остаться",
                style=WARN,
            ),
            f"set-confirm set-dyn {KEEP_IN_VIEW}",
        )
        self._refresh_footer()

    def _close_or_back(self) -> None:
        if self._view == "menu":
            self.app.pop_screen()
        else:
            self._view = "menu"
            self._render_view()

    def _report_open(self) -> bool:
        return self._report_text is not None

    def _dismiss_report(self) -> bool:
        """Закрыть открытый отчёт. `False` — отчёта нет, Esc достаётся дальше по
        лестнице."""
        if self._report_text is None:
            return False
        self._report_text = None
        self._report_section = None
        for w_ in self.query(f".{KEEP_UNTIL_DISMISSED}"):
            parent = w_.parent
            (
                parent
                if parent is not None and parent.has_class("set-slot")
                else w_
            ).remove()
        # Обещание снимается ЗДЕСЬ, а не у вызывающих: путей закрытия три (Esc,
        # реплика, плашка), и футер, обещающий закрытие несуществующего отчёта,
        # — та же ложь, от которой заведён сам маркер.
        self._refresh_footer()
        return True

    def _dismiss_ephemeral(self) -> None:
        """Правило В1 (ревизия владельца): сообщение-итог гаснет на следующем
        осознанном действии. По родовому маркеру, не по перечню id — чинится
        класс. Сносится ОБЁРТКА `.set-slot`, а не только Static: иначе пустые
        обёртки копились бы до `_render_view` (прежняя преамбула флэша этим
        грешила). Преамбула стоит ДО обработчиков, поэтому сообщение, рождённое
        этой же клавишей, свою клавишу переживает."""
        for w_ in self.query(f".{EPHEMERAL}"):
            parent = w_.parent
            if parent is not None and parent.has_class("set-slot"):
                parent.remove()
            else:
                w_.remove()

    def on_click(self, event) -> None:
        """Мышиная половина правила В1 — на КЛИКЕ, не на MouseDown.

        Снос сообщения двигает тело секции вверх (обёртка `.set-slot` живёт в
        `#set-frame`), а `Click` резолвится по раскладке, актуальной на момент
        своей доставки. Гашение на `MouseDown` съедало первый клик: цель
        уезжала из-под курсора между нажатием и кликом — а сворачивание блоков
        «Моделей» мышиное и только мышиное, и каждая успешная запись оставляет
        плашку, то есть это был основной путь, а не край (находка финального
        ревью). На `Click` обработчик цели уже отработал: пользователь получает
        и действие, и погасшее сообщение."""
        self._dismiss_ephemeral()

    def on_key(self, event: events.Key) -> None:
        self._dismiss_ephemeral()
        if event.key in ("up", "down"):
            # Кадр едет за курсором секции. Клавишу НЕ забираем — её разберут
            # редакторы ниже; здесь только заказ доводки после раскладки.
            reveal_focus(self)
        if event.key == "escape":
            if self._dirty_exit_confirm:
                self._dirty_exit_confirm = False
                self._pending_section_delta = None
                for widget in self.query("#set-dirty-confirm"):
                    widget.remove()
                self._refresh_footer()
                self.set_focus(self._dirty_exit_focus)
                self._dirty_exit_focus = None
                event.stop()
                return
            if self._dismiss_report():
                # Первая ступень лестницы: открытый отчёт закрывается СВОИМ Esc,
                # из секции уводит уже следующий (канон вложенности — как у
                # пикеров и confirm-баннеров).
                event.stop()
                self._refresh_footer()
                return
            if self._view == _MCP:
                ed = self._mcp_editor()
                if ed is not None and ed.awaiting_confirm:
                    event.stop()
                    ed.cancel_pending()
                    return
            if self._view == _MODES and self._modes_picker is not None:
                event.stop()  # закрыт только список, секция жива
                self._close_model_picker()
                return
            if self._view == _UPDATE and self._update_awaiting_confirm:
                event.stop()
                self._cancel_update()
                return
            if self._view == _UPDATE and self._reset_awaiting_confirm:
                event.stop()
                self._cancel_reset()
                return
            if self._view == _VOICE and self._voice_awaiting_confirm():
                event.stop()
                self._cancel_voice_confirm()
                self._refresh_voice_hint()  # баннер → подсказка, без пересборки полей
                return
            if self._view == _VOICE:
                ctrl = self._voice_list_ctrl()
                if ctrl is not None and ctrl.is_open:
                    # Esc сворачивает СПИСОК и оставляет курсор в поле (из секции не
                    # выходим): стрелки после него снова водят ленту.
                    event.stop()
                    ctrl.close()  # `close` сам скажет строкой, чем вернуть
                    self._refresh_footer()
                    return
                if self._voice_is_dirty():
                    event.stop()
                    self._ask_dirty_exit()
                    return
            if (
                self._view in _WRITE_GATED_SECTIONS
                and self._pending_env_save is not None
            ):
                # Множество ШИРЕ редакторных: подтверждение перезаписи ${ENV:VAR}
                # висит и над плоской записью секции «Модели» (`agent.model`), а
                # снять его больше некому — Esc там иначе уводит в меню, оставив
                # висеть подтверждение.
                event.stop()
                self._cancel_env_confirm()  # отмена подтверждения, из секции не выходим
                return
            if self._view == _CONNECTION:
                try:
                    ed = self._connection_editor()
                except NoMatches:
                    ed = None
                if ed is not None and ed.escape_presets():
                    event.stop()  # закрыт только пресет-лист адреса, секция жива
                    self._refresh_footer()  # клавиши вернулись секции — футер за ними
                    return
                if ed is not None and ed.is_dirty():
                    event.stop()
                    self._ask_dirty_exit()
                    return
            if self._view == _MODELS:
                ed = self._models_editor()
                if ed is not None and ed.legacy_dirty:
                    event.stop()
                    self._ask_dirty_exit()
                    return
                if ed is not None and ed.escape():
                    self._pending_section_delta = None
                    event.stop()  # закрыто под-состояние секции, из неё не выходим
                    self._refresh_footer()
                    return
            if self._view == _PROFILES and self._profiles_open is not None:
                # Вложенность на всю глубину: сперва своё под-состояние закрывает сам
                # редактор (пикер, форма), и только когда закрывать нечего — под-вид
                # схлопывается к списку профилей. В меню уводит уже следующий Esc.
                event.stop()
                ed = self._models_editor()
                if ed is None or not ed.escape():
                    self._profiles_open = None
                    self._render_view()
                self._pending_section_delta = None
                self._refresh_footer()
                return
            if (
                self._view == _PROFILES
                and self._profiles_name_prompt is not None
            ):
                event.stop()  # отмена ввода имени, из секции не выходим
                self._cancel_profile_name_prompt()
                return
            if (
                self._view == _PROFILES
                and self._profiles_delete_name is not None
            ):
                event.stop()  # снятие подтверждения удаления — тоже своим уровнем
                self._cancel_delete_profile()
                return
            event.stop()
            self._close_or_back()
            return
        if self._dirty_exit_confirm:
            event.stop()
            if event.key == "enter":
                self._dirty_exit_confirm = False
                for widget in self.query("#set-dirty-confirm"):
                    widget.remove()
                if self._pending_section_delta is not None:
                    delta = self._pending_section_delta
                    self._pending_section_delta = None
                    self._switch_section(delta)
                else:
                    ed = self._models_editor()
                    if (
                        self._view == _MODELS
                        and ed is not None
                        and ed.legacy_open
                        and not ed.registry_empty
                    ):
                        ed.escape()
                        self._refresh_footer()
                    else:
                        self._close_or_back()
            return
        if self._view == _MCP:
            ed = self._mcp_editor()
            if ed is not None and ed.awaiting_confirm and event.key != "enter":
                event.stop()  # confirm-баннер висит: глотаем всё, кроме
                return  # enter (→ confirm_pending ниже) и escape (выше)
        if self._view == _UPDATE and self._update_awaiting_confirm:
            event.stop()
            if event.key == "enter":
                self._confirm_update()
            return  # прочие клавиши — no-op, пока висит confirm
        if self._view == _UPDATE and self._reset_awaiting_confirm:
            event.stop()
            if event.key == "enter":
                self._confirm_reset()
            return  # прочие клавиши — no-op, пока висит confirm
        if event.key in ("pageup", "pagedown", "ctrl+pageup", "ctrl+pagedown"):
            event.stop()  # Ctrl+ — тихий алиас для мышечной памяти
            # Любой вопрос блокирует переход секции. Раньше повторный PgDn попадал
            # в `ed.escape()`, снимал подтверждение грязной формы и оставлял
            # отложенный переход без явного решения пользователя.
            if self._view == _MODELS:
                ed = self._models_editor()
                if ed is not None and ed._dirty_exit_pending:
                    return
            if self._view == _VOICE and self._voice_awaiting_confirm():
                return
            delta = -1 if event.key in ("pageup", "ctrl+pageup") else 1
            ed = self._models_editor()
            if ed is not None and ed.form_open and ed._form.is_dirty():
                self._pending_section_delta = delta
                # Свернуть все под-состояния формы: каждый Esc снимает одно.
                collapsed = ed._form.escape()
                while collapsed:
                    collapsed = ed._form.escape()
                ed.escape()
                self._refresh_footer()
                return
            if self._view == _CONNECTION:
                ed = self._connection_editor()
                if ed is not None and ed.is_dirty():
                    self._pending_section_delta = delta
                    self._ask_dirty_exit()
                    return
            if self._view == _VOICE and self._voice_is_dirty():
                self._pending_section_delta = delta
                self._ask_dirty_exit()
                return
            if self._view == _MODELS and ed is not None and ed.legacy_dirty:
                self._pending_section_delta = delta
                self._ask_dirty_exit()
                return
            self._switch_section(delta)
            return
        if (
            event.key == "enter"
            and self._view in _WRITE_GATED_SECTIONS
            and self._pending_env_save is not None
        ):
            event.stop()  # confirm перезаписи ссылки: Enter — подтвердил
            section, values, warn_detail, back_to_menu = self._pending_env_save
            self._pending_env_save = None
            self._do_save(
                section,
                values,
                warn_detail=warn_detail,
                confirmed=True,
                back_to_menu=back_to_menu,
            )
            return
        if event.key in ("tab", "shift+tab"):
            # Клавиша групп (спека §4.1). Съедаем ВСЕГДА, даже когда переходить некуда
            # (§4.2.6): tab/shift+tab — штатные биндинги Textual (focus_next/
            # focus_previous), и оставить их фреймворку значит вернуть рассинхрон «фокус
            # отдельно, курсор отдельно». Групп больше одной пока только в голосовой
            # секции — в остальных клавиша просто молчит.
            #
            # Прежнего смысла «проверить связь» у неё здесь больше нет: проверка ушла на
            # автопробу при уходе с поля (спека §5.2) и на Ctrl+Space (§5.1).
            event.stop()
            event.prevent_default()  # не отдавать focus-next
            if self._view == _VOICE and not self._voice_awaiting_confirm():
                # Пока висит подтверждение (снос/установка), секция глотает всё, кроме
                # Enter и Esc — курсор при незаданном вопросе не ходит.
                self._move_group(1 if event.key == "tab" else -1)
            return
        if (
            event.key in (_KEY_ALT_DOWN, _KEY_CTRL_DOWN)
            and self._view in _PROBE_SECTIONS
        ):
            # Развод клавиш (спека 2026-08-06 §2): раскрытие списка — теперь ЦЕЛИКОМ
            # на Alt+↓ (и тихом синониме Ctrl+↓), Ctrl+Space ниже про список больше не
            # спрашивает. У адреса список есть (пресеты) — свёрнутый раскрываем,
            # раскрытый перечитывать нечего (статика). Тогда клавиша проваливается к
            # прокрутке тела в конце обработчика: `Ctrl+↓` обещан футером как
            # прокрутка, и съесть его молча значило бы соврать (в пробу ей больше не
            # попасть — проба на своих клавишах).
            try:
                ed = self._connection_editor()
            except NoMatches:
                ed = None
            if ed is not None and ed.reload_presets():
                event.stop()
                self._refresh_footer()  # обещания клавиш меняются вместе со списком
                return
        if (
            event.key in (_KEY_CTRL_AT, _KEY_CTRL_SPACE, _KEY_CTRL_AT_SIGN)
            and self._view in _PROBE_SECTIONS
        ):
            # Развод §2: клавиша больше не спрашивает список (см. ветку Alt+↓ выше) —
            # порядок неважен, она всегда «перепроверить связь» (§2.2 спеки). Проба
            # уходит при ЛЮБЫХ полях, в том числе нетронутых: ради этого случая она и
            # заведена — «сервер лежал, поля с тех пор не трогали», где автоматическая
            # проверка по построению уже не повторится (§5.2).
            event.stop()
            if not self._writable():
                return  # read-only: редактора нет, проверять нечего
            from gc_core import config_raw
            from gc_core.onboarding import _valid_endpoint_url

            url = self._connection_editor().peek()[0]
            if (
                url
                and not _valid_endpoint_url(url)
                and not config_raw.env_refs(url)
            ):
                # §2.3: пока список раскрыт, в поле может лежать ФИЛЬТР, а не адрес —
                # бить пробой по нему вслепую значит стучаться на "gr". Пустое поле
                # ведёт себя как раньше (фолбэк на сохранённый адрес — внутри
                # _start_draft_probe); проверка стоит ДО _reset_draft_state/
                # _cancel_autoprobe — иначе отказ стартовать уже погасил бы вердикт
                # «умного Enter» и снял запланированную автопробу (докстринг
                # _cancel_autoprobe).
                #
                # Ссылка ${ENV:VAR}/${FILE:...} — штатное СЫРОЕ значение поля адреса
                # (settings.py читает completions_url без резолва, см. `_raw_value`):
                # `_valid_endpoint_url` бракует и её (нет http(s)-схемы), но это не
                # фильтр — блокировать пробу здесь значило бы навсегда убить ручную
                # перепроверку связи для корп-конфигов на ${ENV:GC_URL} (находка
                # ревью, фикс-раунд 1). Пропускаем гейт и отдаём решение
                # `_start_draft_probe` как есть — она сегодня шлёт ссылку в probe_fn
                # без резолва (тот же пробел жил тут и ДО этой задачи, не наш скоуп).
                self._show_probe_line(
                    Text(
                        "сначала выберите адрес — проверка стучится на него",
                        style=DEEP_DIM,
                    )
                )
                return
            self._reset_draft_state()
            self._cancel_autoprobe()  # запускаем СВОЮ пробу — автопроба лишняя
            self._start_draft_probe(self._view, manual=True)
            return
        if self._view == _MODELS:
            ed = self._models_editor()
            if ed is not None and self._route_registry_editor(ed, event):
                return
        if event.key == _SAVE_KEY and self._view == _CONNECTION:
            event.stop()
            if not self._writable():
                self._notice("конфиг недоступен для записи")
                return
            values = self._collect_values(_CONNECTION)
            if values is None:
                return
            if not values:
                self._close_or_back()
                return
            self._do_save(_CONNECTION, values)
            return
        if event.key == "enter" and self._view in _PROBE_SECTIONS:
            section = self._view
            if section == _CONNECTION:
                try:
                    ed = self._connection_editor()
                except NoMatches:
                    ed = None
                # автопробу здесь НЕ трогаем: на пустом адресе список раскрыт (канон
                # 2026-08-17 — чистая установка), и если Enter поглощён им, своя проба
                # не стартует — без автопробы вердикт связи не появился бы вовсе, до
                # первого Ctrl+Space или правки с Enter.
                if ed is not None and ed.enter_presets():
                    self._refresh_footer()  # список свернулся — футер снова про секцию
                    return  # пресет-лист открыт — подставил/открыл, не сабмит
            # Enter в обычном поле принадлежит самому полю. Сохранение формы
            # выполняется единообразно через Ctrl+S; список адресных пресетов
            # остаётся единственным вложенным состоянием, которое Enter выбирает.
        if event.key == "enter" and self._view == _UPDATE:
            event.stop()
            self._run_update_action()
            return
        if event.key in ("up", "down") and self._view == _UPDATE:
            event.stop()
            n = len(self._update_actions)
            if n > 1:
                self._update_idx = (
                    self._update_idx + (1 if event.key == "down" else -1)
                ) % n
                self._render_view()
            return
        if self._view == _VOICE:
            # Esc — в верхнем esc-диспетче (он видит клавишу первым).
            if self._voice_awaiting_confirm():
                event.stop()
                if event.key == "enter":
                    if self._voice_awaiting_remove:
                        self._voice_confirm_remove()
                    else:
                        self._voice_confirm_install()
                elif event.key in ("up", "down"):
                    self._move_voice(
                        -1 if event.key == "up" else 1
                    )  # навигация отменяет
                return  # прочие клавиши — no-op, пока висит confirm
            if event.key == _SAVE_KEY:
                event.stop()
                if self._voice_pane == "cloud":
                    self._voice_cloud_save()
                elif self._voice_fields_shown():
                    self._voice_local_language_save()
                return
            if event.key in (_KEY_ALT_DOWN, _KEY_CTRL_DOWN):
                # До общей навигации: явно раскрыть список поля, ничего не печатая —
                # второй путь назад после Esc (первый — печать, см. reopen_for_typing).
                # Что именно значит «раскрыть», решает сам список (`reload`): у языка —
                # только если свёрнут, у моделей — ещё и перечитать с сервера. Развод
                # клавиш (спека 2026-08-06 §2): это ЕДИНСТВЕННЫЙ путь к списку теперь —
                # Ctrl+Space про него больше не спрашивает (ветка ниже).
                #
                # Вердикт перечитывание НЕ трогает: список идёт на /models, а вердикт
                # отвечает за /audio/transcriptions — успех первого про второй ничего
                # не говорит, и зелёная ✓ на неработающем endpoint'е была бы ложью
                # (заодно подавив настоящую пробу через `_voice_probe_values`).
                #
                # Раскрывать нечего — клавиша проваливается к прокрутке тела (тот же
                # довод, что у адреса подключения выше).
                ctrl = self._voice_list_ctrl()
                if ctrl is not None and ctrl.reload():
                    event.stop()
                    self._refresh_footer()
                    return
            if event.key in (_KEY_CTRL_AT, _KEY_CTRL_SPACE, _KEY_CTRL_AT_SIGN):
                # Проба — только в облачной панели (§2.5 спеки 2026-08-06): локальная
                # адреса не спрашивает (канон §9.2), проверять там нечего — клавиша
                # съедается молча, ни в какие глобальные биндинги не проваливаясь.
                # Список поля клавиша больше не спрашивает вовсе (ушёл на Alt+↓ выше):
                # порядок с ним неважен (§2.2) — раскрыт список или нет, пробе всё
                # равно.
                event.stop()
                if self._voice_pane == "cloud":
                    self._voice_probe()
                return
            if event.key in ("up", "down"):
                event.stop()
                self._move_voice(-1 if event.key == "up" else 1)
                return
            if event.key == "delete" and not self._voice_focus_in_editor():
                event.stop()  # в полях Del правит текст — не перехватываем
                self._voice_request_remove()
                return
            if event.key == _KEY_CTRL_U and not self._voice_focus_in_editor():
                event.stop()  # тот же гейт по фокусу, что у Del
                self._voice_update_engine()
                return
            if event.key == _KEY_CTRL_U and self._voice_clear_token():
                # В полях та же клавиша стирает СОХРАНЁННЫЙ токен — как на
                # «Подключении» (там `Ctrl+U` пришёл из develop). Голосовая панель
                # показывает секрет такой же маской, а убрать его было нечем вовсе:
                # поле пустое, а пустое значит «оставить как было». Гейт (то ли поле,
                # есть ли что стирать) живёт в редакторе — здесь только маршрут.
                event.prevent_default()
                event.stop()
                self._refresh_voice_hint()
                self._refresh_footer()
                return
            if event.key == "enter":
                event.stop()
                self._voice_enter()
                return
        if self._view == _MODES and self._modes_picker is not None:
            event.prevent_default()
            event.stop()
            self._model_picker_key(event.key)
            return
        if self._view == _MODES:
            from gc_tui.toggles import modes_section_names

            rows = modes_section_names()
            if event.key in ("up", "down"):
                step = -1 if event.key == "up" else 1
                self._modes_idx = (self._modes_idx + step) % len(rows)
                self._render_view()
                event.prevent_default()
                event.stop()
                return
        if event.key in ("left", "right", "enter") and self._view == _MODES:
            event.stop()
            if event.key != "enter":
                self._modes_apply(-1 if event.key == "left" else 1)
            elif self._modes_row_opens_picker():
                self._open_model_picker()
            return
        if event.key in ("left", "right") and self._view == _LOOK:
            event.stop()
            self._look_apply(-1 if event.key == "left" else 1)
            return
        if self._view == _PROFILES:
            self._profiles_disk_cache = {}  # нажатие — свежий взгляд на диск
            if self._profiles_open is not None:
                # Под-вид держит клавиатуру ЦЕЛИКОМ и обрывает обработчик даже там,
                # где сам клавишу не взял: под ним лежит список профилей со своими
                # Del/r/Space, и провалившийся туда `Del` снёс бы профиль вместо
                # записи реестра. PgUp/PgDn и Esc сюда не доходят — их разбирают
                # гейты выше.
                ed = self._models_editor()
                if ed is not None and self._route_registry_editor(ed, event):
                    return
                if event.key in _SCROLL_KEYS:
                    # Прокрутка тела — не клавиша списка под под-видом: её не
                    # проваливаем, а исполняем здесь. Иначе футер, обещающий
                    # «Ctrl+↑/↓ — прокрутка», врал бы в открытом профиле.
                    self._scroll_body_key(event)
                    return
                if ed is not None:
                    self._route_profile_file_view(ed, event)
                return
            if self._profiles_name_prompt is not None:
                # Ввод имени владеет клавиатурой: своя клавиша здесь одна — Enter
                # (Esc снят выше, глобальным гейтом). Остальные НЕ съедаем: правка
                # поля (backspace, ←/→, Home/End) живёт биндингами Input, а они
                # разбираются ПОСЛЕ всплытия события к App — съеденная тут клавиша
                # до них не дойдёт, и имя стало бы нестираемым. Клавиши секции при
                # этом молчат сами: ↑/↓ у Input биндинга не имеют.
                if event.key == "enter":
                    event.stop()
                    self._submit_profile_name_prompt()
                return
            if self._profiles_delete_name is not None:
                event.stop()  # confirm-баннер висит: Enter — удалить,
                if event.key == "enter":  # прочее — no-op (канон MCP)
                    self._confirm_delete_profile()
                return
            rows = self._profiles_rows()
            if event.key in ("up", "down"):
                event.stop()
                step = -1 if event.key == "up" else 1
                self._profiles_idx = (self._profiles_idx + step) % len(rows)
                self._render_view()
                return
            row = self._profiles_row_under_cursor(rows)
            # Две клавиши — два разных глагола (решение В1): Enter «входит внутрь»
            # объекта под курсором — тот же канон, что у меню секций и списка MCP,
            # и с приходом редактора профиля внутрь наконец есть куда войти. Смена
            # состояния остаётся за Space (канон базы: MCP «вкл/выкл», отметка в
            # post-update) — им профиль активируется ниже. Слить их в один кортеж
            # значило бы отдать Space открытие и оставить активацию без клавиши,
            # которую обещает футер.
            if event.key == "enter":
                event.stop()
                if row["kind"] == "profile":
                    self._open_profile(row)
                elif row["kind"] == "add":
                    self._begin_profile_name_prompt("create")
                else:
                    # «— без профиля» — не файл, открывать нечего (деактивация —
                    # Space). Молчание было бы ложью футера, обещающего «Enter —
                    # открыть» на этой строке тоже: no-op + подсказка (решение В1).
                    self._title_note(_PROFILE_NONE_NOTHING_TO_OPEN)
                return
            # Клавиши-объекты (буквы, пробел, Del) — по фокусу (§5.3): в поле ввода
            # они печатаются и правят текст, а не командуют секцией. Гейт стоит ОДИН
            # раз на входе в их группу, а не при каждой — так следующая клавиша
            # получит правило даром (тот же приём, что у отмены confirm в «Моделях»).
            if isinstance(self.focused, Input):
                return
            if event.key == "space":
                event.stop()
                if row["kind"] == "profile":
                    self._activate_profile(row["name"], row["ref"])
                elif row["kind"] == "none":
                    self._deactivate_profile()
                return
            if event.key in ("n", "т", "N", "Т"):
                event.stop()
                self._begin_profile_name_prompt("create")
                return
            if event.key in ("r", "к", "R", "К"):
                event.stop()
                if row["kind"] == "profile":
                    self._begin_profile_name_prompt("rename", row["name"])
                return
            if event.key == "delete":
                event.stop()
                if row["kind"] == "profile":
                    self._request_delete_profile(row["name"])
                return
        if self._view == _MCP and self._writable_or_mcp():
            ed = self._mcp_editor()
            if ed is not None:
                if ed.awaiting_confirm:
                    if event.key == "enter":
                        event.stop()
                        self._mcp_result_notice(ed.confirm_pending())
                        return
                    event.stop()
                    return  # прочие клавиши секции — no-op, пока висит confirm (escape уже обработан выше)
                if event.key in ("up", "down"):
                    event.stop()
                    ed.move(-1 if event.key == "up" else 1)
                    return
                if event.key == "space":
                    event.stop()
                    hint = ed.toggle()
                    if hint:
                        self._title_note(
                            hint
                        )  # у заголовка, 3с (было _notice — постоянный низ)
                    return
                if event.key == "delete" and not isinstance(
                    self.focused, Input
                ):
                    # Единая клавиша удаления объекта под курсором (§5.3), тот же гейт
                    # по фокусу, что у голосовой секции — полей ввода здесь сейчас нет,
                    # но правило общее.
                    event.stop()
                    ed.request_remove()
                    return
                if event.key == _KEY_CTRL_U:
                    event.stop()
                    ed.begin_update()
                    return
                if event.key == "enter":
                    event.stop()
                    ed.begin_enter()
                    return
        if event.key in ("up", "down") and self._view == _CONNECTION:
            event.stop()
            try:
                ed = self._connection_editor()
            except NoMatches:
                ed = None
            if ed is not None:
                leaving_id = getattr(
                    self.focused, "id", None
                )  # с какого поля уходим
                ed.move(-1 if event.key == "up" else 1)
                # move() двигает только курсор, значений не трогает (их правки ловит
                # on_input_changed) — обесценивать им вердикт и летящую пробу нельзя.
                # Иначе вторая стрелка внутри латентности роняет пробу первой, повтор
                # не уходит (снимок совпал), и строка застревает на «проверяю…»; а
                # стрелка во время пробы Enter молча съедает сохранение, висящее у неё
                # в on_done. Ровно поэтому в мастере уход с поля поколение не бампает.
                self._cancel_env_confirm()  # но подтверждение перезаписи ссылки уход с поля снимает
                self._update_dirty()
                self._autoprobe_on_leave(ed, leaving_id)
                self._refresh_footer()  # состав клавиш зависит от того, раскрыт ли список
            return
        if event.key == _KEY_CTRL_U and self._view == _CONNECTION:
            # Очистка сохранённого токена. Клавишу приходится перехватывать здесь:
            # Input держит на ней delete_left_all, а поле токена пустое (секрет
            # живёт в конфиге, в поле — маска), и штатное поведение там вхолостую.
            # clear_token() сама решает, её ли это случай; не её — клавиша идёт
            # дальше, к Input.
            try:
                ed = self._connection_editor()
            except NoMatches:
                ed = None
            if ed is not None and ed.clear_token():
                event.prevent_default()
                event.stop()
                # Очистка меняет и то, что уйдёт в запрос, и то, что уйдёт в файл:
                # летящая проба, её вердикт, снимок дребезга и подтверждение перезаписи
                # ссылки относятся к прежней форме. Печать в поле проходит этот же
                # сброс через `on_input_changed` — но Ctrl+U стирает то, чего в поле
                # нет (секрет живёт под маской), и события Changed не порождает.
                self._reset_draft_state()
                self._update_dirty()
                self._refresh_footer()
            return
        if event.key == "space" and self._view == _CONNECTION:
            # Галка «без авторизации» — Space переключает её, только если курсор
            # секции сейчас на позиции галки (toggle_no_auth() решает сама по
            # своему _idx). Не стопим клавишу, если курсор не там: пробел обязан
            # допечататься в сфокусированный Input (например, токен).
            try:
                ed = self._connection_editor()
            except NoMatches:
                ed = None
            if ed is not None and ed.toggle_no_auth():
                event.stop()
            return
        if self._view == "menu":
            if event.key in ("up", "down"):
                event.stop()
                self._move_menu(-1 if event.key == "up" else 1)
                return
            if event.key == "enter":
                event.stop()
                self._open_section(self._menu_idx)
                return
        # Запасной вариант, ПОСЛЕ всех претендентов: Ctrl+↑/↓ прокручивают тело
        # секции постранично. Стоят в конце, а не в начале: у develop Ctrl+↓
        # раскрывает список в редакторах проб, и перехват сверху отнял бы у него
        # клавишу — сюда управление доходит только если её никто не разобрал.
        if event.key in _SCROLL_KEYS:
            self._scroll_body_key(event)
            return

    def _scroll_body_key(self, event: events.Key) -> None:
        """`Ctrl+↑`/`Ctrl+↓` — прокрутка тела секции страницами (обещана футером)."""
        event.stop()
        scroll_body(self, _SEL_SET_SCROLL, -1 if event.key == "ctrl+up" else 1)

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._view in _PROBE_SECTIONS:
            self._reset_draft_state()
        if (
            self._view in (_CONNECTION, _MODELS)
            or self._profiles_open is not None
        ):
            # Печать в адресе меняет фильтр пресет-листа, а с ним и обещания клавиш
            # (подставить ↔ оставить свой адрес) — футер догоняет её же событием.
            # У формы записи реестра ровно те же два списка и та же развилка — в
            # открытом профиле это та же форма, значит и то же правило.
            self._refresh_footer()
        if self._view in _EDITOR_SECTIONS:
            self._update_dirty()
        if self._view == _VOICE:
            self._voice_values_changed()
            # Печать в «Языке» меняет фильтр списка, а с ним и обещания клавиш
            # (подставить ↔ сохранить как есть) — футер обязан догонять её же событием.
            self._refresh_footer()
