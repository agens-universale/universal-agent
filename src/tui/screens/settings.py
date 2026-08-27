"""Хаб настроек /settings (спека B, модель A+; визуальный контракт §2-3).

Меню секций → Enter открывает тело секции (свап идиомой .set-dyn), Esc — назад
в меню (из меню — закрыть экран), ←/→ — соседняя секция БЕЗ выхода в меню
(только когда фокус не в текстовом поле). Push-экран поверх TraceScreen."""
from __future__ import annotations

from dataclasses import replace

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import Input, Rule, Static
from rich.text import Text

from tui.editors.models_section import _FOOTER_PROFILE_BACK
from tui.palette import ACCENT, DEEP_DIM, DIM, ERR, INK, OK, WARN
from tui.form_frame import (EPHEMERAL, KEEP_IN_VIEW, KEEP_UNTIL_DISMISSED,
                            cap_frame_height, reveal, reveal_focus)
from tui.widgets.loading_wave import WAVE_INTERVAL, WAVE_SEQ, wave_glyph

SECTIONS = ("Подключение", "Модели", "Профили", "MCP-серверы", "Режимы работы", "Обновление")

_CONNECTION, _MODELS, _PROFILES, _MCP, _MODES, _UPDATE = 0, 1, 2, 3, 4, 5
_EDITOR_SECTIONS = {_CONNECTION}           # редакторные: writable, dirty-tracked, submit
_PROBE_SECTIONS = {_CONNECTION}            # server-probe: ручная проверка, глиф из config-probe
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
    # «↑/↓ — навигация» снята ПЕРВОЙ — курсор и так виден подсветкой поля, а не третьей
    # (не называть клавишу) — до неё дело не дошло, снятого сегмента хватило.
    _CONNECTION: ("Enter — сохранить  ·  Alt+↓ — показать список  ·  "
                  "Ctrl+Space — проверить связь  ·  PgUp/PgDn — секции  ·  Esc — меню"),
    # Футера секции «Модели» здесь нет: он позиционный и живёт в самой секции
    # (`tui/editors/models_section.py`) — Enter значит разное на строке записи,
    # строке резервной модели и в каждой из её под-форм.
    # «Обновить версию», а не просто «обновить»: Ctrl+U ставит пакет заново (меняет
    # систему), а Alt+↓ только перечитывает — одним словом на двоих они неразличимы (§5.4).
    _MCP: "↑/↓ · Enter — установить · Space — вкл/выкл · Ctrl+U — обновить версию · Del — удалить · PgUp/PgDn — секции · Esc",
    _MODES: "↑/↓ — выбор · ←/→/Enter — переключить · PgUp/PgDn — секции · Esc — меню",
    _UPDATE: "↑/↓ · Enter — выполнить · PgUp/PgDn — секции · Esc — меню",
    # Раскладка — решение юзера В1 (`docs/specs/2026-08-10-profiles-b-plan.md`),
    # выведенное из канона базы: Enter = «войти внутрь» (меню «открыть», MCP
    # «установить»), Space = переключение состояния (MCP «вкл/выкл», post-update
    # «отметить»). Активация профиля — смена состояния, поэтому её клавиша Space,
    # а Enter открывает редактор профиля (Task 6).
    # Стиль строки — MCP (точки-разделителями, без «Esc»): канонный хвост
    # «↑/↓ — выбор · … · Esc — меню» с пятью клавишами не влезает в две строки на
    # 64 колонках (бюджет `_FOOTER_MIN_COLS`).
    _PROFILES: ("↑/↓ · Enter — открыть · Space — активировать · n — создать · "
                "r — переименовать · Del — удалить · PgUp/PgDn — секции"),
}
_FOOTER_READONLY = "PgUp/PgDn — секции  ·  Esc — меню"
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
_FOOTER_PRESET_LIST = ("↑/↓ — выбор  ·  Enter — подставить  ·  Esc — свернуть"
                       "  ·  Ctrl+Space — проверить связь  ·  PgUp/PgDn — секции")
# Набран свой адрес — в списке не осталось строк: подставлять нечего (`pick()` вернёт
# None), Enter лишь сворачивает список, оставляя набранное, а ↑/↓ снова водят курсор
# секции (`ConnectionEditor.move` отдаёт их ленте). Глагол — тот же, что в хинте под
# рамкой (`preset_list.PRESET_HINT_NO_MATCH`): обе строки видны одновременно.
_FOOTER_PRESET_LIST_NO_MATCH = ("↑/↓  ·  Enter — оставить свой адрес  ·  Esc — свернуть"
                                "  ·  Ctrl+Space — проверить связь  ·  PgUp/PgDn — секции")
# Список свёрнут, но курсор НЕ на адресе (Токен/Прокси/галка): Alt+↓ там молчит —
# `ConnectionEditor.on_url_field` гейтит ровно позицию адреса (`reload_presets`), и
# `_FOOTERS[_CONNECTION]` в этом состоянии обещал бы неработающую клавишу (находка
# финального ревью 2026-08-06, D). Тот же текст без сегмента про Alt+↓.
_FOOTER_CONNECTION_FIELD = ("Enter — сохранить  ·  "
                            "Ctrl+Space — проверить связь  ·  PgUp/PgDn — секции  ·  Esc — меню")
# Строка уровня (последняя в секции «Профили») — тот же позиционный приём, что и
# «Подключение» выше: обычный `_FOOTERS[_PROFILES]` обещал бы «Enter —
# активировать» там, где Enter (и ←/→) на самом деле переключают уровень —
# ложь того же рода, что нашёл финальный обзор 2026-08-06, D.
# Строка «— без профиля» — не объект, а действие «снять текущий выбор», и глагол
# у неё СВОЙ. Общий футер секции обещал здесь «активировать», пока подсказка под
# списком в том же кадре говорила «деактивировать»; курсор при входе в секцию
# стоит ровно на этой строке, так что противоречие было первым, что видит глаз.
# Тот же позиционный приём, что у «Подключения» (_FOOTER_CONNECTION_FIELD).
_FOOTER_PROFILES_NONE = ("↑/↓ — выбор  ·  Space — деактивировать  ·  n — создать  ·  "
                         "PgUp/PgDn — секции  ·  Esc — меню")
# Профиль не выбран — снимать нечего, и футер молчит о действии вовсе: обещать
# «деактивировать» там, где деактивировать некого, — та же ложь другим глаголом.
_FOOTER_PROFILES_NONE_EMPTY = "↑/↓ — выбор  ·  n — создать  ·  PgUp/PgDn — секции  ·  Esc — меню"
# Курсор на токене и есть что стирать. Сегмент занимает место «Alt+↓ — показать
# список» — тот обещается только на адресе, эти двое на разных полях и в одной
# строке не встречаются, поэтому бюджету ширины ничего не грозит.
_FOOTER_CONNECTION_TOKEN = ("Enter — сохранить  ·  Ctrl+U — очистить  ·  "
                            "Ctrl+Space — проверить связь  ·  PgUp/PgDn — секции  ·  Esc — меню")
# Строка уровня (последняя в секции «Профили») — тот же позиционный приём, что и
# «Подключение» выше: обычный `_FOOTERS[_PROFILES]` обещал бы «Space —
# активировать» там, где ←/→/Enter на самом деле переключают уровень — ложь того
# же рода, что нашёл финальный обзор 2026-08-06, D.
# «n — создать» здесь остаётся: клавиша секционная (не про строку под курсором) и
# работает в том числе в ПУСТОМ состоянии, где строка уровня — единственная.
# Футер, промолчавший о ней там, оставил бы создание первого профиля без входа —
# это второй класс лжи футера («молчание о работающей клавише»).
# Строка-действие «+ добавить профиль…»: обещаем только то, что на ней работает.
_FOOTER_PROFILES_ADD = ("↑/↓ — выбор  ·  Enter — создать  ·  "
                        "PgUp/PgDn — секции  ·  Esc — меню")
_FOOTER_PROFILES_LEVEL = ("↑/↓ — выбор  ·  ←/→/Enter — переключить  ·  n — создать  ·  "
                          "PgUp/PgDn — секции  ·  Esc — меню")
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
_FOOTER_MIN_COLS = 64
_TITLE_NOTE_SEC = 3.0     # «ничего не изменено» у заголовка гаснет само


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


def fit_overlay_suffix(names: list[str], width: int) -> str:
    """Суффикс секций-оверлеев профиля («+security+summary»), усечённый ЦЕЛЫМИ
    именами под `width` (тот же приём, что и `fit_mode_pairs`, но без разделителя
    между именами). `width <= 0` — строка ещё не смонтирована — без усечения."""
    if not names:
        return ""
    full = "".join(f"+{n}" for n in names)
    if width <= 0 or len(full) <= width:
        return full
    tail = "+…"
    out: list[str] = []
    for n in names:
        candidate = "".join(f"+{x}" for x in out + [n]) + tail
        if len(candidate) <= width:
            out.append(n)
        else:
            break
    return ("".join(f"+{x}" for x in out) + tail) if out else "+…"


def _empty_anchor(value) -> bool:
    """Пустой якорь секции файла профиля: `slots: {}` (то, что остаётся после
    снятия последнего ключа) либо голое `slots:` без значения. Переопределением
    такая секция не является — переопределять ей нечем (§11).

    Скаляр пустым якорем не бывает НИКОГДА, каким бы он ни был: `level: max` —
    полноценное переопределение, и трактовать его как контейнер значило бы
    потерять единственную витрину ключей, форм для которых нет."""
    return value is None or (isinstance(value, (dict, list)) and not value)


class SettingsScreen(Screen):
    def __init__(self, *, config, config_path, on_apply, probe_fn=None,
                 auth_probe_fn=None) -> None:
        super().__init__()
        self._config = config or {}
        self._config_path = config_path
        self._raw = self._read_raw()    # значения полей ИЗ ФАЙЛА, без резолва (см. _read_raw)
        self._origins = self._read_origins()   # происхождение полей: чем перекрыто, что действует
        self._on_apply = on_apply
        self._pending_env_save = None   # (section, values, warn_detail) — ждёт confirm перезаписи ссылки
        self._view: object = "menu"     # "menu" | int(0..5)
        self._menu_idx = 0
        self._modes_idx = 0             # курсор строки подэкрана «Режимы работы»
        self._profiles_idx = 0          # курсор строки подэкрана «Профили»
        self._profiles_disk_cache: dict = {}   # дисковые чтения секции «Профили» на один проход UI
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
        self._menu_pass_snapshot = None   # (текст, документ) цели записи на один проход меню
        from tui.services.settings_probe import probe as _default_probe
        from tui.services.settings_probe import probe_auth as _default_auth_probe
        self._probe_fn = probe_fn or _default_probe
        # Отдельно от _probe_fn: тому нужна models-цепочка (глифы хаба показывают
        # модели сервера), а вердикт об авторизации обязан идти строго по
        # chat/completions — у публичного /models он получился бы ложным.
        #
        # Подменили probe_fn — значит забрали сеть целиком, и вторым каналом наружу
        # экран не ходит: иначе частично застабленный экран молча стучится в интернет.
        self._auth_probe_fn = auth_probe_fn or probe_fn or _default_auth_probe
        self._probe_config = None       # ProbeResult сохранённых значений → глифы хаба
        self._config_gen = 0            # generation config-probe: устаревшие результаты отбрасываются
        self._draft_gen = 0             # generation draft-probe: независим от config-probe
        self._autoprobe_timer = None    # таймер автопробы секции «Подключение» (см. _cancel_autoprobe)
        self._last_probed_snapshot = None   # ConnectionEditor.probe_snapshot() последней пробы
        self._probe_in_flight = False
        self._note_timer = None         # автогашение title-note «ничего не изменено»
        self._wave_timer = None         # таймер анимации загрузки (глифы хаба)
        self._wave_i = 0                # кадр «бегущего огонька»
        # draft-слот: результат последней проверки введённых значений
        # (probe-строка рендерится напрямую в _on_draft_probe; умный Enter: ok → без повтора)
        self._draft_checked = None
        # секция «Обновление»: список действий текущего рендера + курсор (None → дефолт
        # при следующем _mount_update_section — верхнее действие, «Обновить агента»)
        self._update_actions: list[str] = []
        self._update_idx = None
        self._update_check_result = None    # CheckResult последней проверки (Task 7)
        self._update_checking = False       # воркер check_for_update в полёте
        self._update_awaiting_confirm = False   # confirm-баннер обновления/рестарта висит
        self._reset_awaiting_confirm = False    # confirm-баннер «сбросить конфиг к умолчанию»
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
                yield Static(self._title_text(), id="header-left", markup=False)
                yield Static(self._version_text(), id="header-right", markup=False)
            scroll = VerticalScroll(id="set-scroll")
            scroll.can_focus = False
            with scroll:
                with Vertical(id="set-frame"):
                    yield Static("Настройки", id="set-title", markup=False)
            yield Static(self._footer_text(), id="set-footer", markup=False)

    def _version_text(self) -> str:
        """Строка версии в шапке. Короткая форма (`dev`, а не полная метка): строка
        тесная. Но правило то же, что в секции «Обновление» — из исходников номер не
        показываем, он остался от установки пакета и коду не соответствует."""
        import app_version
        return f"gc-ua · {app_version.display_version()}"

    def _title_text(self) -> str:
        if self._view == "menu":
            return "Настройки"
        return f"Настройки · {SECTIONS[self._view]}"

    def _footer_text(self) -> str:
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
        if self._view == _CONNECTION and self._presets_open():
            return (_FOOTER_PRESET_LIST if self._presets_have_selection()
                    else _FOOTER_PRESET_LIST_NO_MATCH)
        if self._view == _CONNECTION:
            # Список свёрнут: Alt+↓ обещаем, только пока курсор на адресе — на «Токене»/
            # «Прокси» клавиша молчит (`on_url_field` гейтит ровно эту позицию,
            # находка финального ревью 2026-08-06, D), и обещать её там значило бы врать.
            if self._connection_on_url():
                return _FOOTERS[_CONNECTION]
            return (_FOOTER_CONNECTION_TOKEN if self._connection_can_clear_token()
                    else _FOOTER_CONNECTION_FIELD)
        if self._view == _PROFILES:
            if self._profiles_open is not None:
                # Открытый профиль — тот же редактор форм, что и «Модели», и футер у
                # него тот же позиционный: экран его не считает, а спрашивает. Пока
                # тело не смонтировано — обещаем только возврат к списку:
                # `_FOOTER_READONLY` соврал бы про «Esc — меню».
                ed = self._models_editor()
                return (self._profile_footer(ed) if ed is not None
                        else _FOOTER_PROFILE_BACK)
            if self._profiles_name_prompt is not None:
                return (_FOOTER_PROFILE_NAME_RENAME
                        if self._profiles_name_prompt["mode"] == "rename"
                        else _FOOTER_PROFILE_NAME_CREATE)
            if self._profiles_on_level_row():
                return _FOOTER_PROFILES_LEVEL
            if self._profiles_on_none_row():
                return (_FOOTER_PROFILES_NONE if (self._config.get("profile") or None)
                        else _FOOTER_PROFILES_NONE_EMPTY)
            if self._profiles_on_add_row():
                # На строке-действии общий футер врал бы всеми клавишами объектов
                # (открыть/Space/r/Del на ней не работают) — канон «недоступное
                # не обещаем», тот же приём, что у строки уровня.
                return _FOOTER_PROFILES_ADD
            return _FOOTERS[_PROFILES]
        return _FOOTERS.get(self._view, _FOOTER_READONLY)

    def _profile_footer(self, ed) -> str:
        """Футер под-вида профиля: строку даёт редактор (она позиционная), а
        обещание `v` дописывает ЭКРАН — просмотрщик его, и гасит обещание его же
        настройка (§11a). Редактор про файловый просмотр не знает ничего.

        Обещаем ровно там, где клавиша работает: под висящим выбором (форма,
        пикер, вопрос) её нет — у тех состояний и варианта с `v` не заведено."""
        footer = ed.footer_text()
        if not (ed.showing_list and self._file_view_settings().enabled):
            return footer
        from tui.editors.models_section import _FOOTERS_WITH_VIEW
        return _FOOTERS_WITH_VIEW.get(footer, footer)

    def _file_view_settings(self):
        """Настройки встроенного просмотрщика — тем же фабричным методом и из того
        же ключа конфига, что у `/view` главного экрана: вторая сборка разошлась бы
        с ней на первой же правке схемы."""
        from tui.services.file_view import FileViewSettings
        return FileViewSettings.from_mapping((self._config or {}).get("tui_file_view"))

    def _connection_editor(self):
        """Свежесмонтированный редактор секции «Подключение».

        ПОСЛЕДНИЙ матч, а не NoMatches на пустом: после `_render_view` старый редактор
        ещё в дереве (`remove()` отложенный), а `query_one` на двух матчах не падает,
        а молча отдаёт ПЕРВЫЙ, обречённый. Здесь это стоило бы дорого: через этот же
        редактор идёт сохранение секции, то есть в конфиг уехали бы значения прошлого
        рендера (находка 2026-08-07)."""
        from tui.editors.connection import ConnectionEditor
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
            self.query_one("#set-footer", Static).update(self._footer_text())
        except NoMatches:
            pass

    def on_mount(self) -> None:
        # Вердикт стартового авто-чека (app) — общий на сессию: хаб показывает статус сразу,
        # а секция не ходит в сеть второй раз (Enter на «Проверить обновления» — форсит).
        self._update_check_result = getattr(self.app, "_update_check_result", None)
        endpoint, _a, _v, _m, _p = self._probe_args_from_config()
        self._probe_in_flight = bool(endpoint)   # первый кадр меню сразу с …, без — → ✓ мелькания
        self._render_view()
        self._start_config_probe()
        self.call_after_refresh(cap_frame_height, self, "#set-scroll")

    def on_resize(self, event: events.Resize) -> None:
        # Потолок рамки зависит только от высоты окна и хрома — пересчитывается на
        # ресайз, а не на каждый кадр секции (см. form_frame).
        cap_frame_height(self, "#set-scroll")

    # --- сырые значения полей (файл — источник для редакторов и сверки «не изменено») ---
    def _read_raw(self) -> dict:
        """Значения полей визарда ИЗ ФАЙЛА конфига, как записаны (без резолва ${ENV:VAR}).

        В self._config они уже резолвнуты (config_modules.env_interpolation), и показать
        или записать их обратно в файл значит подменить ссылку на переменную её значением —
        секрет открытым текстом в файле, который коммитят. Экран правит ФАЙЛ, значит и
        читать должен файл; резолвнутый конфиг остаётся источником только для probe-запросов
        (они проверяют действующий credential)."""
        import config_raw
        from onboarding import FIELD_ANCHOR_PATHS
        if not self._config_path:
            return {}
        values = config_raw.read_values(self._config_path, FIELD_ANCHOR_PATHS.values())
        return {leaf: values[dotted] for leaf, dotted in FIELD_ANCHOR_PATHS.items()}

    def _read_origins(self) -> dict:
        """Происхождение полей визарда: что в базовом файле, чем перекрыто, что действует.

        load_config кладёт поверх базового файла machine-managed и проектный оверлеи —
        экран правит базовый файл, и без этого молчал бы о том, что правка не подействует."""
        from config_origin import origins
        from onboarding import FIELD_ANCHOR_PATHS
        if not self._config_path:
            return {}
        by_dotted = origins(self._config_path, FIELD_ANCHOR_PATHS.values())
        return {leaf: by_dotted[dotted] for leaf, dotted in FIELD_ANCHOR_PATHS.items()}

    def _origin(self, key: str):
        """Origin поля или None (путь конфига не задан)."""
        return self._origins.get(key)

    def _raw_value(self, key: str) -> str:
        """Сырое значение поля строкой; сентинел шаблона / отсутствие → "" (не задано)."""
        from onboarding import MODEL_SENTINEL, TOKEN_SENTINEL, URL_SENTINEL
        v = self._raw.get(key)
        if not isinstance(v, str) or v in (URL_SENTINEL, TOKEN_SENTINEL, MODEL_SENTINEL):
            return ""
        return v

    # --- значения для меню (правый столбец, dim) ---
    def _row_value(self, i: int) -> Text:
        from onboarding import display_secret, display_value, env_ref_status, TOKEN_SENTINEL
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
                mode = display_secret(auth)   # ссылка + статус (переменной нет / подставилась)
            else:
                mode = "Bearer"
            proxy = self._raw_value("proxy")
            proxy_part = f" · прокси" if proxy else ""
            v.append(f"{host} · {mode}{proxy_part}", style=DIM)
        elif i == _MODELS:
            # Реестр есть — превью про него («N в реестре · default …»); реестра нет
            # — прежнее превью легаси-поля. Оба факта — ФАЙЛОВЫЕ (решение 6).
            from tui.editors.models_section import registry_preview
            document = self._target_document()
            if document.get("models"):
                v.append(registry_preview(document), style=DIM)
            else:
                model = self._raw_value("model")
                v.append(display_value(model) if model else "—", style=DIM)
        elif i == _PROFILES:
            name = self._config.get("profile") or None
            v.append(name if name else "не выбран", style=DIM)
        elif i == _MCP:
            try:
                import mcp_runtime.cli as mcp_mod
                n = len(mcp_mod.wired_names())
            except Exception:
                n = 0
            if n:
                v.append(f"{n} подключено", style=OK)
            else:
                v.append("нет подключённых", style=DIM)
        elif i == _MODES:
            from tui.toggles import TOGGLES, toggle_state_base
            # Превью меню — детальные пары (preset-тоггл rigor показывается витриной
            # в самой секции, не дублируется в одну строку меню). Скоуп — только
            # "rigor"-семья: тогглы «чем работаем» (/level, /profiles) живут в
            # своей секции «Профили», не здесь.
            pairs = [f"{spec.label}: {toggle_state_base(self.app, name)}"
                     for name, spec in TOGGLES.items()
                     if spec.group == "rigor" and not spec.preset_keys]
            v.append(fit_mode_pairs(pairs, self._modes_preview_width()), style=DIM)
        else:
            v.append(self._version_text().split("·", 1)[-1].strip(), style=DIM)
        return v

    def _modes_preview_width(self) -> int:
        """Ширина региона `.set-rowtext` строки «Режимы работы» — граница усечения
        превью целыми парами (fit_mode_pairs). До монтирования этой строки меню
        (первый рендер) региона ещё нет — 0, что трактуется как «без усечения»."""
        try:
            rows = list(self.query(".set-menurow"))
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
        cls = "set-menurow set-dyn" + (" selected" if i == self._menu_idx else "")
        frame.mount(Horizontal(
            Static(self._menu_rowtext(i, i == self._menu_idx), classes="set-rowtext"),
            Static(self._menu_glyph_text(i), classes="set-glyph"),
            classes=cls,
        ))

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
            self._wave_timer = self.set_interval(WAVE_INTERVAL, self._wave_tick)

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
            self._refresh_wave_glyphs()      # только глифы (лёгкий кадр, без ре-рендера строк)
        else:
            self._stop_wave()

    def _refresh_wave_glyphs(self) -> None:
        for i, row in enumerate(self.query(".set-menurow")):
            if i not in _EDITOR_SECTIONS:
                continue
            try:
                row.query_one(".set-glyph", Static).update(self._menu_glyph_text(i))
            except NoMatches:
                continue

    def on_unmount(self) -> None:
        self._stop_wave()

    def refresh_update_glyph(self) -> None:
        """Стартовый авто-чек прилетел (app зовёт с UI-потока) — подтянуть его вердикт и
        перерисовать строку меню. Свой вердикт (ручная проверка) не затираем."""
        if self._update_check_result is None:
            self._update_check_result = getattr(self.app, "_update_check_result", None)
        if self._view == "menu":
            self._refresh_menu_rows()

    def _target_snapshot(self) -> tuple[str, dict]:
        """(текст, документ) цели записи за ОДНО чтение на проход отрисовки меню.

        Строку «Модели» спрашивают минимум дважды за кадр (превью и глиф), а глифу
        нужен ещё и текст — для валидации. Без снимка это четыре чтения файла и
        разбор YAML заново на каждое ↑/↓ в меню. Кэш прохода — тот же приём, что у
        `_profiles_disk_cache` секции «Профили», и сбрасывается там же, где рисуется
        меню: правка файла извне видна с первого же кадра."""
        if self._menu_pass_snapshot is None:
            from pathlib import Path
            import config_raw
            text = ""
            if self._config_path:
                try:
                    text = Path(self._config_path).read_text(encoding="utf-8")
                except OSError:
                    text = ""
            self._menu_pass_snapshot = (text, config_raw.parse_document(text))
        return self._menu_pass_snapshot

    def _target_document(self) -> dict:
        """Сырой документ цели записи (базовый config.yml) — ФАЙЛОВОЕ состояние секции
        «Модели» (решение 6 волны B): реестр редактируется в файле, а не в резолве."""
        return self._target_snapshot()[1]

    def _models_glyph(self) -> tuple[str, str]:
        """Вердикт строки «Модели» — про ФАЙЛ, а не про сеть (потому секция и вышла из
        probe-волны). Реестр непуст: сломанные ссылки слотов/привязок → `!`; реестр,
        в котором ни одна запись не резолвит адрес, → `—` (валиден, но не работает —
        та же дыра схемы, о которой секция говорит строкой); иначе `✓`. Реестра нет —
        прежний вердикт легаси-поля (модель есть в списке сервера).

        Чужая ошибка конфига (опечатка в посторонней секции) валит фазу 1 и красила бы
        `!` именно здесь — за неё эта строка не отвечает: `—`, «вердикта нет»."""
        text, document = self._target_snapshot()
        if document.get("models"):
            from config import _app_config_from_text
            from config_modules.model_sections_validation import validate_model_sections
            from tui.editors.models_section import registry_resolves_any_address
            try:
                cfg = _app_config_from_text(text)
            except ValueError:
                return "—", DEEP_DIM         # файл не проходит схему — но не из-за реестра
            try:
                validate_model_sections(cfg)
            except ValueError:
                return "!", ERR              # ссылки слотов/привязок сломаны — это наше
            if not registry_resolves_any_address(document, self._probe_args_from_config()[0]):
                return "—", DEEP_DIM         # адреса нет ни у записей, ни в «Подключении»
            return "✓", OK
        r = self._probe_config
        model = self._config.get("model") or ""
        if not model or r is None or r.models is None:
            return "—", DEEP_DIM
        ids = [m.id for m in r.models]
        return ("✓", OK) if model in ids else ("—", DEEP_DIM)   # не в списке → —, не !

    def _menu_glyph(self, i: int) -> tuple[str, str]:
        """(глиф, style) для строки меню. … в полёте; ✓/!/— по §2 контракта.
        Глиф — вердикт probe о ДЕЙСТВУЮЩИХ (резолвнутых) значениях: он про то, работает
        ли конфиг, а не про то, что записано в файле (это показывает столбец значения)."""
        if i in _EDITOR_SECTIONS:
            if self._probe_in_flight:
                return "…", DEEP_DIM
            r = self._probe_config
            if i == _CONNECTION:
                from onboarding import TOKEN_SENTINEL
                endpoint, _a, _v, _m, _p = self._probe_args_from_config()
                if not endpoint:
                    return "—", DEEP_DIM
                if r is None:
                    return "—", DEEP_DIM
                # Красный — только про подключение. Содержательный отказ про модель
                # (живой GigaCode: 404 «проверьте адрес и имя модели») сюда не
                # попадает: имя модели — забота своей строки, у неё свой глиф.
                if r.offline:
                    return "!", ERR                  # сервер недоступен СЕЙЧАС
                if r.http_status is None:
                    # Ответа нет, но и до сервера мы не доходили: сорвалась сама
                    # проверка (фолбэк `_start_config_probe`). Утверждать за сервер
                    # нечего — строка секции про этот же результат пишет «не удалось
                    # проверить», и хаб не имеет права быть категоричнее её.
                    return "—", DEEP_DIM
                auth = self._raw.get("Authorization")
                if auth == TOKEN_SENTINEL:
                    return "—", DEEP_DIM             # токен не задан (шаблон) — нечем подтвердить
                # Вердикт о токене читаем через общий словарь смыслов — тот же, что у
                # строки секции и у реплики после сохранения: три собственных прочтения
                # одного результата уже разъезжались (хаб верил публичному /models).
                # Токен есть, а вердикта о нём нет — молчим («—»), а не гадаем.
                from tui.services.probe_presentation import token_accepted
                accepted = token_accepted(r, has_token=bool(auth))
                if accepted is None:
                    return "—", DEEP_DIM
                return ("✓", OK) if accepted else ("!", ERR)
        if i == _MODELS:
            return self._models_glyph()
        if i == _MCP:
            try:
                import mcp_runtime.cli as mcp_mod
                return ("✓", OK) if mcp_mod.wired_names() else ("—", DEEP_DIM)
            except Exception:
                return "—", DEEP_DIM
        if i == _UPDATE:
            r = self._update_check_result
            if r is not None:
                if r.status == "update":
                    return "↑", ACCENT
                if r.status == "current":
                    return "✓", OK
                return "—", DEEP_DIM              # unknown/не удалось — нет вердикта
            if getattr(self.app, "_update_available", False):
                return "↑", ACCENT               # вердикта сессии нет — персист-флаг
            if self._update_checking or getattr(self.app, "_update_checking", False):
                return "…", DEEP_DIM              # стартовый чек в полёте — вердикт вот-вот будет
            return "—", DEEP_DIM
        if i == _PROFILES:
            return ("✓", OK) if self._config.get("profile") else ("—", DEEP_DIM)
        return "—", DEEP_DIM

    # --- рендер (полная замена тела фрейма, кроме персистентного #set-title) ---
    def _render_view(self) -> None:
        self._profiles_disk_cache = {}   # новый кадр — данные секции «Профили» перечитываются
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
        frame = self.query_one("#set-frame", Vertical)
        title = self.query_one("#set-title", Static)
        # Снять фокус ДО remove: detached focused-виджет рвёт последующую доставку
        # клавиш (тот же баг и то же лекарство, что у OnboardingScreen._mount_step_body —
        # секции без своего поля (Режимы/MCP/Обновление) не перехватывают фокус заново,
        # и PgUp/PgDn после них переставали находить секцию). Секции со своим Input
        # (Адрес/Модель) перефокусируют себя сами при монтировании.
        self.set_focus(None)
        # Убираем ВСЁ динамическое, а не только .set-dyn: корни редакторов несут
        # класс onb-dyn (общий с онбордингом), поэтому class-фильтр их пропускал —
        # редактор оставался при Esc (мусор + меню внизу). Чистим по факту детей.
        for w in list(frame.children):
            if w is not title:
                w.remove()
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
            if self._view == _MODES:
                self.call_after_refresh(self._refresh_modes_hint)
                self.call_after_refresh(self._refresh_modes_rows)
            elif self._view == _PROFILES and self._profiles_open is not None:
                self.call_after_refresh(self._refresh_profile_overlay)
            elif self._view == _PROFILES:
                self.call_after_refresh(self._refresh_profiles_hint)
                self.call_after_refresh(self._refresh_profiles_rows)
        if self._report_text is not None:
            self._mount_report(self._report_text)   # пережил перерисовку своей секции
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
            # пересчёт «вслепую» опасен там, где содержимое приезжает из сети (список
            # моделей) — он лёг бы между раскрытием списка и приездом строк и оставил бы
            # футер на «совпадений нет» до следующего события.
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
        title.display = bool(content.plain if isinstance(content, Text) else content)

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
        self._note_timer = self.set_timer(_TITLE_NOTE_SEC, self._clear_title_note)

    def _clear_title_note(self) -> None:
        self._note_timer = None
        self._update_dirty()        # вернуть истинное состояние (имя / «изменено»)

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

    # --- probe-инфраструктура (спека B2.1: слоты + generation) ---
    def _probe_args_from_config(self):
        from onboarding import MODEL_SENTINEL, TOKEN_SENTINEL, URL_SENTINEL
        c = self._config
        url = c.get("completions_url") or ""
        endpoint = url if url and url != URL_SENTINEL else ""
        auth = (c.get("chat_completion_headers") or {}).get("Authorization")
        if auth in (TOKEN_SENTINEL, ""):
            auth = None
        proxy = c.get("proxy") or None
        model = c.get("model") or ""
        if model == MODEL_SENTINEL:   # плейсхолдер шаблона — не слать его как имя модели
            model = ""
        return endpoint, auth, bool(c.get("use_ssl", True)), model, proxy

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
            from tui.services.settings_probe import ProbeResult, probe_connection
            try:
                result = self._probe_fn(
                    endpoint, auth, verify, model, config_proxy=proxy,
                )
            except Exception as exc:
                result = ProbeResult("unknown", None, None, 0, f"внутренняя ошибка проверки: {exc}")
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
                    auth_result = probe_connection(endpoint, auth, verify, model,
                                                   config_proxy=proxy,
                                                   probe_fn=self._auth_probe_fn)
                except Exception:
                    auth_result = None      # не смогли — хаб про токен просто промолчит
            if auth_result is not None and auth_result.auth_state is not None:
                # Вливаем вердикт в ТОТ ЖЕ результат, а не держим вторым полем: два
                # поля про одну пробу расходятся при первом же обновлении одного из
                # них. Список моделей остаётся — по нему живёт глиф «Модель».
                result = replace(result, auth_state=auth_result.auth_state)
            self.app.call_from_thread(self._on_config_probe, result, gen=gen)

        self.run_worker(_work, thread=True, name="settings-config-probe")

    def _on_config_probe(self, result, *, gen: int) -> None:
        if gen != self._config_gen or not self.is_mounted:
            return                                   # устаревший/посмертный — отбросить
        self._probe_in_flight = False
        self._stop_wave()
        self._probe_config = result
        self._refresh_menu_rows()

    def _refresh_menu_rows(self) -> None:
        if self._view != "menu":
            return
        self._menu_pass_snapshot = None   # новый проход — цель перечитывается один раз
        for i, row in enumerate(self.query(".set-menurow")):
            if i >= len(SECTIONS):
                # Транзиентный двойной комплект строк: отложенный call_after_refresh
                # (carry-fix выше) может сработать ДО того, как Prune старых 5 строк
                # применится — query видит старые+новые вперемешку (до 10), а
                # SECTIONS всего len(SECTIONS) — без guard это IndexError.
                continue
            try:
                row.query_one(".set-rowtext", Static).update(
                    self._menu_rowtext(i, i == self._menu_idx))
                row.query_one(".set-glyph", Static).update(self._menu_glyph_text(i))
            except NoMatches:
                continue   # дети строки ещё не смонтированы (первый тик on_mount)
            (row.add_class if i == self._menu_idx else row.remove_class)("selected")
        if self._probe_in_flight:
            self._start_wave()      # пока идёт probe — крутим волну (стоп в _wave_tick)

    # --- draft-probe секции (Ctrl+Space / умный Enter, спека v2 §3.1) ---
    def _probe_line_text(self, r, section: int) -> Text:
        """Текст probe-строки по канону §3.1. «Подключение» — совокупный вердикт
        сервер+токен одной строкой (спек A4); прочие секции — общий формат из
        r.detail (его формирует interpret_auth: «доступен» / «доступ без токена» /
        «токен принят» / «сервер требует авторизацию»)."""
        if r is None:
            return Text("— не проверено", style=DEEP_DIM)
        if section == _CONNECTION:
            # Смысл авторизации — из r.auth_state (ставит interpret_auth); r.detail —
            # только текст. Сырой config-probe (auth_state=None, без differential
            # auth) «ok» читаем нейтрально — токен не проверяли, утверждать
            # «принят» нечестно.
            if r.verdict == "ok":
                if r.auth_state == "accepted":
                    t = Text("✓ сервер доступен · ", style=OK)
                    t.append("✓ токен принят", style=OK)
                    return t
                if r.auth_state == "not_required":
                    return Text("✓ сервер доступен · авторизация не требуется", style=OK)
                return Text("✓ сервер доступен", style=OK)
            if r.verdict == "rejected" and r.http_status == 401:
                if r.auth_state == "required":
                    # токена не было вовсе (сервер отклонил control-запрос без него) —
                    # не «токен отклонён» (нечего было отклонять)
                    t = Text("✓ сервер доступен · ", style=OK)
                    t.append(f"⚠ {r.detail}", style=WARN)
                    return t
                t = Text("✓ сервер доступен · ", style=OK)
                t.append("✗ токен отклонён (401)", style=WARN)
                return t
            if r.offline:
                # Винить сервер можно только когда до него не дошли. Раньше сюда
                # попадал любой unknown без статуса — в том числе наш внутренний
                # сбой, и причина терялась вместе с detail.
                return Text("✗ сервер недоступен", style=WARN)
            return Text(f"? не удалось проверить: {r.detail}", style=WARN)
        if r.verdict == "ok":
            msg = f"{r.detail} · {r.latency_ms} ms"
            return Text(f"✓ {msg}", style=OK)
        if r.verdict == "rejected":
            return Text(f"✗ {r.detail}", style=ERR)
        return Text(f"⚠ не удалось проверить: {r.detail}", style=WARN)

    def _start_draft_probe(self, section: int, on_done=None) -> None:
        """Differential-probe «Подключения»: контрольный запрос БЕЗ токена определяет,
        требует ли сервер авторизацию; только при 401 проверяем сам токен. Так
        честно отличаем «сервер не требует токен» от «токен принят/отклонён».

        Endpoint и token берутся из ПОЛЯ (ConnectionEditor.peek(), draft), а не из
        сохранённого конфига — иначе проверялась бы несуществующая комбинация
        «новый адрес + старый токен»."""
        from onboarding import authorization_header
        endpoint_cfg, auth_cfg, verify, model, proxy_cfg = self._probe_args_from_config()
        url, auth_idx, token_raw, proxy_draft = self._connection_editor().peek()
        endpoint = url or endpoint_cfg               # поле пусто → действующий (для ссылок)
        if not endpoint:
            self._set_probe_line(Text("сначала настройте адрес — проверка стучится на него",
                                      style=DEEP_DIM))
            return
        if auth_idx == 1:
            token = None                             # без авторизации
        elif token_raw:
            token = authorization_header(token_raw)  # введённый токен
        elif self._connection_editor().token_cleared:
            token = None                             # Ctrl+U: сохранённый стёрт ещё до записи
        else:
            token = auth_cfg                         # поле пусто → действующий токен
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
        self._set_probe_line(Text("проверяю…", style=DEEP_DIM))

        def _work() -> None:
            from tui.services.settings_probe import ProbeResult, probe_connection
            try:
                result = probe_connection(endpoint, token, verify, model,
                                          config_proxy=proxy,
                                          probe_fn=self._auth_probe_fn)
            except Exception as exc:
                result = ProbeResult("unknown", None, None, 0, f"внутренняя ошибка проверки: {exc}")
            self.app.call_from_thread(self._on_draft_probe, result, gen=gen,
                                      section=_CONNECTION, on_done=on_done)

        self.run_worker(_work, thread=True, name="settings-draft-probe")

    def _on_draft_probe(self, result, *, gen, section, on_done) -> None:
        if gen != self._draft_gen or not self.is_mounted or self._view != section:
            return
        self._draft_checked = result
        self._set_probe_line(self._probe_line_text(result, section))
        if on_done is not None:
            on_done(result)

    def _set_probe_line(self, text: Text) -> None:
        try:
            self.query_one(".set-probe", Static).update(text)
            return
        except NoMatches:
            pass
        try:
            frame = self.query_one("#set-frame", Vertical)
        except NoMatches:
            return
        frame.mount(Static(text, classes="set-hint set-dyn set-probe"))

    def _reset_draft_state(self) -> None:
        self._draft_checked = None
        self._cancel_env_confirm()  # правка обесценивает и подтверждение перезаписи ссылки
        self._draft_gen += 1        # правка значения обесценивает летящий draft-probe (config-probe не трогаем)
        # Снимок живёт ровно столько, сколько проба, которая его записала: её
        # результат сейчас обесценен, значит значения снова НЕ проверены. Оставить
        # снимок значило бы запретить гейтом дребезга повтор пробы, которой уже не
        # будет, — и строка вердикта осталась бы на «проверяю…» навсегда.
        self._last_probed_snapshot = None
        if self._view in _PROBE_SECTIONS:
            try:
                self.query_one("#set-footer", Static).update(self._footer_text())
            except NoMatches:
                pass

    def _smart_enter_decide(self, section: int, values: dict, r) -> None:
        """Проверка ⊥ Сохранение: Enter всегда сохраняет (блок — только формальная
        невалидность, её ловит _collect_values→submit). Вердикт сервера — информация.

        Текст реплики — у `probe_presentation.save_warning`: разбирать `verdict`
        здесь значило бы завести третье прочтение одного результата (после строки
        секции и глифа хаба), и оно уже разъехалось — «rejected» объявлялось
        отклонённым токеном даже когда сервер писал про имя модели."""
        if not values:
            self._title_note("ничего не изменено")
            return
        from tui.services.probe_presentation import save_warning
        self._do_save(section, values, warn_detail=save_warning(r))

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
        from config import _default_config_path
        if not target:
            return False
        try:
            return Path(target).resolve() != Path(_default_config_path()).resolve()
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
    _PROFILE_FILE_WRITE_REFUSAL = (
        "✗ не {done}: {reason} — используется пакетный шаблон "
        "(настройте GC_HOME или запустите gc-ua --init-config)"
    )

    _SECRET_FIELD = "Authorization"   # действующее значение секрета маскируем, не печатаем как есть

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
        from onboarding import display_secret
        return display_secret(effective if isinstance(effective, str) else str(effective))

    def _field_hint_specs(self, key: str) -> list[tuple[str, str]]:
        """(текст, css-класс) подписей под полем — только когда есть что сказать.

        Ссылка ${ENV:VAR} → её статус. Поле перекрыто оверлеем → действующее значение
        и источник. Случаи складываются: в файле ссылка И поле перекрыто → обе строки.
        Обычное значение из базового файла → подписей нет, экран чист.

        Данными, а не монтированием: те же подписи рисует у себя секция «Модели» —
        легаси-редактор уехал внутрь её тела, а знание о происхождении полей живёт
        здесь (`_origins`)."""
        from onboarding import display_value, env_ref_status
        specs: list[tuple[str, str]] = []
        raw = self._raw_value(key)
        if env_ref_status(raw):
            specs.append((display_value(raw), "set-hint set-dyn"))
        origin = self._origin(key)
        if origin is not None and origin.is_overridden:
            from config_origin import overlay_label
            source = overlay_label(origin)
            shown = self._effective_display(key, origin.effective)
            specs.append((f"⚠ действует {shown} — перекрыто {source}",
                          "set-hint set-dyn set-warnflash"))
        return specs

    def _mount_field_hints(self, frame: Vertical, key: str) -> None:
        for text, cls in self._field_hint_specs(key):
            frame.mount(Static(text, classes=cls))

    def _mount_section_body(self, frame: Vertical, section: int) -> None:
        from onboarding import TOKEN_SENTINEL
        if section in _WRITE_GATED_SECTIONS and not self._writable():
            frame.mount(Static(
                "конфиг недоступен для записи: используется пакетный шаблон "
                "(настройте GC_HOME или запустите gc-ua --init-config)",
                classes="set-hint set-dyn"))
            return
        if section == _CONNECTION:
            from tui.editors.connection import ConnectionEditor
            auth = self._raw.get("Authorization")
            header = None if auth == TOKEN_SENTINEL else auth   # None=сентинел; ""=без авт.; иначе значение
            from config_modules.endpoint_presets import endpoint_presets_from_config
            frame.mount(ConnectionEditor(
                url_current=self._raw_value("completions_url"),
                auth_header=header,
                proxy_current=self._raw_value("proxy"),
                presets=endpoint_presets_from_config(self._config or {})))
            self._mount_field_hints(frame, "completions_url")
            self._mount_field_hints(frame, "Authorization")
            self._mount_field_hints(frame, "proxy")
            self._mount_shadow_hint(frame)
            frame.mount(Static(self._probe_line_text(self._probe_config, _CONNECTION),
                               classes="set-hint set-dyn set-probe"))
        elif section == _MODELS:
            from pathlib import Path
            from config import _default_config_path
            from config_modules.endpoint_presets import endpoint_presets_from_config
            from tui.editors.models_section import ModelsSectionEditor
            endpoint, auth, verify, _m, proxy = self._probe_args_from_config()
            try:
                template = Path(_default_config_path()).read_text(encoding="utf-8")
            except OSError:
                template = ""       # шаблона нет — якорь секции синтезирует сам writer
            frame.mount(ModelsSectionEditor(
                target=self._config_path, template=template,
                effective_url=endpoint, effective_auth=auth, verify=verify,
                config_proxy=proxy,
                presets=endpoint_presets_from_config(self._config or {}),
                active_level=str((self._config or {}).get("level") or "default"),
                legacy_editor_factory=self._make_legacy_model_editor,
                legacy_hints=self._field_hint_specs("model"),
                on_change=self._models_changed, on_notice=self._models_notice,
                on_legacy_save=self._save_legacy_model))
        elif section == _MCP:
            from tui.editors.mcp_section import McpSectionEditor
            from tui.services.mcp_manager import McpManager
            mgr = McpManager(user_config=self._config)
            frame.mount(McpSectionEditor(manager=mgr, on_change=self._mcp_changed))
        elif section == _MODES:
            self._mount_modes_section(frame)
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
        from tui.editors.models_section import ModelsSectionEditor
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
        self._cancel_env_confirm()
        if ed.awaiting_confirm:
            event.stop()
            if event.key == "enter":
                ed.confirm_pending()    # escape снят выше, глобальным гейтом
                self._refresh_footer()
            return True
        if event.key in ("up", "down"):
            event.stop()
            ed.move(-1 if event.key == "up" else 1)
            self._refresh_footer()
            return True
        if event.key in ("alt+down", "ctrl+down"):
            # Карта проб §2.5: подбор списка (форма — свой, легаси-редактор —
            # свой). Клавиша съедается безусловно: свёртывания у списка
            # моделей нет, обещание в футере гейтится позицией курсора.
            event.stop()
            ed.alt_down()
            self._refresh_footer()
            return True
        if event.key in ("ctrl+at", "ctrl+space", "ctrl+@"):
            # Проба есть только у формы записи; на списке и в легаси-редакторе
            # её нет (§5.2) — клавиша съедается молча, ничего не обещая.
            event.stop()
            ed.probe()
            return True
        if event.key == "ctrl+u":
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
        if event.key == "delete" and not isinstance(self.focused, Input):
            event.stop()
            ed.request_remove()
            self._refresh_footer()
            return True
        if event.key == "space":
            if ed.space():
                event.stop()            # галка формы; иначе пробел печатается
            return True
        if event.key in ("alt+n", "alt+т", "alt+N", "alt+Т") or (
                event.key in ("n", "т", "N", "Т")
                and not isinstance(self.focused, Input)):
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
        from tui.editors.model import ModelEditor
        editors = list(self.query(ModelEditor))
        if not editors:
            raise NoMatches("ModelEditor не смонтирован")
        return editors[-1]

    def _make_legacy_model_editor(self):
        """Легаси-редактор модели (`agent.model`) — тот же, что стоял в секции
        «Модель» до поглощения: в пустом реестре он и есть секция, при живом реестре
        — под-форма строки «резервная модель»."""
        from model_picker import fetch_models
        from tui.editors.model import ModelEditor
        from tui.services.model_list_cache import make_cached_fetch
        # За списком моделей ходим ДЕЙСТВУЮЩИМИ (резолвнутыми) адресом и токеном;
        # current/active — сырые: именно они пишутся в файл.
        endpoint, auth, verify, _m, proxy = self._probe_args_from_config()
        model = self._raw_value("model")
        origin = self._origin("model")
        return ModelEditor(
            current=model, endpoint=endpoint, auth=auth, verify=verify,
            fetch=make_cached_fetch(fetch_models), active=model, config_proxy=proxy,
            # Перекрытую модель в списке нельзя звать «текущей»: действует оверлей,
            # а подпись ниже говорит именно это — экран не должен спорить сам с собой.
            active_overridden=bool(origin is not None and origin.is_overridden))

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
            # Своим каналом секции, а не репликой в слоте заголовка: заголовка у
            # «Моделей» нет (`_section_title`), и трёхсекундная реплика поднимала бы
            # его вместе с отступом — тело прыгнуло бы на два ряда вниз и обратно
            # прямо под курсором, которым только что нажали Enter. Отказом записи
            # двумя строками выше секция отвечает тем же `_notice`.
            self._notice("ничего не изменено")
            return
        # Пустое состояние — это САМА секция, и запись из неё завершает работу с ней
        # (возврат в хаб, как у прежней «Модели»). Под-форма резервной открыта из
        # списка записей — ей возвращаться туда же, откуда пришли.
        editor = self._models_editor()
        self._do_save(_MODELS, values,
                      back_to_menu=editor is None or editor.registry_empty)

    def _models_changed(self) -> None:
        """Секция записала в файл: сырые значения и происхождение перечитать, конфиг
        приложения пересобрать — витрины (футер, `/config`, превью хаба) обязаны
        показывать записанное, не прежнее."""
        self._raw = self._read_raw()
        self._menu_pass_snapshot = None   # файл изменился — снимок прохода протух
        try:
            self._on_apply()
            self._config = getattr(self.app, "_config", None) or self._config
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
        Источник — effective_config(): сессионный выбор профиля обязан отразиться в
        подсказке немедленно, как и everywhere в TUI."""
        cfg = self.app.effective_config() if hasattr(self.app, "effective_config") else self._config
        profile_name = cfg.get("profile") or None
        if profile_name:
            return f"действует профиль {profile_name} · эти поля — база для незаданного"
        if cfg.get("models"):
            return "действует реестр моделей · эти поля — база для незаданного"
        return None

    def _mount_shadow_hint(self, frame: Vertical) -> None:
        text = self._shadow_hint_text()
        if text is not None:
            frame.mount(Static(text, classes="set-hint set-dyn"))

    # --- секция «Режимы работы» (дизайн 2026-07-18-settings-work-modes-design §3-4) ---
    def _modes_is_child(self, name: str) -> bool:
        """Ребёнок пресета (`spec.dotted` ∈ чьего-то `preset_keys`) — родовой
        сигнал ревизии 7 (флэт: иерархия только цветом, без маркера/отступа —
        `_modes_tree_glyph` упразднён). Мирроринг
        `command_palette.py::_owning_preset` (тот же приём, без общего
        модуля — см. `_modes_columns`/`command_palette.py::_toggle_columns`)."""
        from tui.toggles import TOGGLES
        spec = TOGGLES[name]
        if spec.preset_keys:
            return False
        return any(spec.dotted in pspec.preset_keys
                   for pspec in TOGGLES.values() if pspec.preset_keys)

    def _modes_columns(self) -> tuple[int, int, int, int]:
        """(label_w, base_w, extra_w, value_w) — общие колонки строк секции
        (выравнивание ревизия 5, спека §4.2/§6: тот же контракт и тот же
        split базы/резерва, что у палитры — `command_palette.py::_toggle_columns`).
        label_w — макс. ширина головы строки: «name · label» и у пресета, и у
        ребёнка (ревизия 7 — flat, маркера/отступа нет, оба head в одном
        формате) / голый `spec.label` у прочих (standalone, не пресет и не
        чей-либо ребёнок) + 3 + 2 на курсор; base_w — макс. ширина БАЗОВОГО
        трека (2*n-1 ячеек, без 4-й позиции); extra_w — макс. ширина custom-
        резерва (3 ячейки у preset-тоггла — 4-я позиция зарезервирована в
        Settings всегда, спека §4.2; 0 у single-key тогглов — у них 4-й
        позиции нет вовсе); value_w (ревизия 6 п.3) — фикс-ширина колонки
        подписи значения: макс. длина имени значения по ВСЕМ тогглам секции,
        ПЛЮС `PRESET_CUSTOM` — пресет-строка может показать «custom», хотя
        оно не входит в `spec.names` ни одного тоггла. Точный контракт брал
        бы max только по строкам, где custom реально достижим (preset-
        тогглы); здесь — упрощённо max по ВСЕМ именам+custom сразу (custom
        никогда не оказывается ДЛИННЕЕ реальных имён значений — упрощение не
        сужает колонку ни для одной строки, только потенциально резервирует
        на символ-два больше, чем строго необходимо, зато без разбора
        preset/не-preset)."""
        from tui.toggles import PRESET_CUSTOM, TOGGLES, group_toggle_names
        names = group_toggle_names("rigor")
        widths = []
        for name in names:
            spec = TOGGLES[name]
            if spec.preset_keys or self._modes_is_child(name):
                widths.append(len(f"{name} · {spec.label}"))
            else:
                widths.append(len(spec.label))
        label_w = max(widths) + 3 + 2
        base_w = 0
        extra_w = 0
        value_names = [PRESET_CUSTOM]
        for name in names:
            spec = TOGGLES[name]
            base_w = max(base_w, 2 * len(spec.values) - 1)
            if spec.preset_keys:
                extra_w = max(extra_w, 3)
            value_names.extend(spec.names)
        value_w = max(len(v) for v in value_names)
        return label_w, base_w, extra_w, value_w

    def _modes_row_width(self) -> int:
        """Ширина строки тоггла внутри рамки (padding 0 1 — минус 2), тот же приём,
        что и `_modes_hint_width`: при монтировании региона ещё нет (0 → хвост
        оверрайда попадает в текст целиком), carry-fix `_refresh_modes_rows`
        досчитывает all-or-nothing усечение после layout-прохода. Последний матч
        по классу — свежесмонтированная строка (на случай транзиентного двойного
        комплекта, как в `_refresh_menu_rows`)."""
        widgets = list(self.query(".set-row"))
        if not widgets:
            return 0
        return max(0, widgets[-1].region.width - 2)

    def _mode_row_text(self, name: str, selected: bool) -> Text:
        from tui.toggles import (
            TOGGLES, padded_track, toggle_state, toggle_state_base, PRESET_CUSTOM,
        )
        spec = TOGGLES[name]
        current = toggle_state_base(self.app, name)   # секция правит глобал → трек = глобал
        label_w, base_w, extra_w, value_w = self._modes_columns()
        is_child = self._modes_is_child(name)
        if spec.preset_keys:
            head = f"{name} · {spec.label}"
            extra = True if current == PRESET_CUSTOM else False
        elif is_child:
            # Голова ребёнка несёт СВОЁ имя команды по образцу родителя
            # («gate · <label>»), не голый label.
            head = f"{name} · {spec.label}"
            extra = None
        else:
            head = spec.label
            extra = None
        track = padded_track(spec, current, extra_lit=extra, base_w=base_w, extra_w=extra_w)
        t = Text()
        t.append("▸ " if selected else "  ", style=ACCENT)   # курсор — конвенция меню/«Обновления»
        # Ревизия 7 (спека §4.2): пресет — head целиком bold INK (без слеша);
        # ребёнок — flat, БЕЗ маркера и БЕЗ отступа (тот же левый край, что и
        # родитель) — имя+описание целиком DIM, иерархия только цветом.
        if spec.preset_keys:
            t.append(head.ljust(label_w - 2), style=f"bold {INK}")
        elif is_child:
            t.append(head.ljust(label_w - 2), style=DIM)
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
        # подпись значения ребёнка — тоже DIM (трек между head и подписью —
        # семантические цвета, не задет), мирроринг
        # `command_palette.py::_format_toggle_row`.
        t.append(current.ljust(value_w), style=DIM if is_child else INK)
        # §6.2 (ревизия 2026-07-21 вечер): сессионный оверрайд — хвостом В ЭТОЙ ЖЕ
        # строке, не отдельной (живая примерка: отдельная строка не читалась —
        # непонятно, к какому тогглу относится). Слова «глобально» нет: значение
        # выше — уже глобал (секция его редактор). All-or-nothing по ширине (тот
        # же канон, что и scope-хвост палитры §6.1): не влезло — хвост уходит
        # целиком, ядро строки (курсор+label+трек+значение) видимо всегда.
        # На пресете effective = резолв сессионной пары (low/medium/high/custom).
        effective = toggle_state(self.app, name)
        if effective != current:
            tail = f"   · в текущей сессии: {effective}"
            width = self._modes_row_width()
            if width <= 0 or len(t.plain) + len(tail) <= width:
                t.append(tail, style=DEEP_DIM)
        return t

    def _mount_modes_section(self, frame: Vertical) -> None:
        from tui.toggles import TOGGLES, group_toggle_names
        names = group_toggle_names("rigor")
        self._modes_idx = min(self._modes_idx, len(names) - 1)
        for i, name in enumerate(names):
            selected = i == self._modes_idx
            # Разделитель групп (ревизия 7, спека §4.2): родовой по
            # `preset_keys`, ОБЩИЙ приём с палитрой
            # (`command_palette.py::_group_sep_count`/`_build_footer`) — черта
            # перед КАЖДЫМ preset-родителем, который не первая строка секции.
            # Сегодня в TOGGLES одна preset-группа (`rigor`) → не рисуется.
            if i > 0 and TOGGLES[name].preset_keys:
                frame.mount(Rule(classes="set-group-sep"))
            # Без set-spaced: плотность B — тогглы группы впритык (общий пульт),
            # воздух даёт разделитель групп (margin-top .set-group-sep).
            cls = "set-row set-dyn" + (" selected" if selected else "")
            frame.mount(Static(self._mode_row_text(name, selected), classes=cls))
        # Подсказка выделенного тоггла — ВНУТРИ рамки (конвенция «Обновления»:
        # пояснительная строка в карточке под строками-опциями, класс set-hint).
        frame.mount(Static(self._modes_hint_text(), classes="set-hint set-dyn set-modes-hint"))

    def _modes_hint_width(self) -> int:
        """Ширина hint-строки внутри рамки (padding 0 1 — минус 2). Строка монтируется
        заново на КАЖДЫЙ рендер (frame полностью перебирается) — региона нет до
        layout-прохода этого же кадра; carry-fix `_refresh_modes_hint` (тот же приём,
        что и `_refresh_menu_rows`) досчитывает усечение постфактум. Последний матч
        по классу — свежесмонтированный (транзиентный старый ещё не отсоединён)."""
        widgets = list(self.query(".set-modes-hint"))
        if not widgets:
            return 0
        return max(0, widgets[-1].region.width - 2)

    def _modes_hint_text(self) -> str:
        """Подсказка выделенного тоггла внутри рамки (канон §6, без клавиш): значение
        + подсказка ГЛОБАЛЬНОГО состояния. Для строки-пресета в состоянии custom —
        динамический custom-хинт (спека §5)."""
        from tui.toggles import (
            _fit_value_hint, TOGGLES, toggle_state_base, preset_custom_hint, PRESET_CUSTOM,
            group_toggle_names,
        )
        names = group_toggle_names("rigor")
        if not names:
            return ""
        name = names[self._modes_idx]
        spec = TOGGLES[name]
        current = toggle_state_base(self.app, name)
        if current in spec.names:
            val = spec.values[spec.names.index(current)]
            return _fit_value_hint(val.value, val.hint, self._modes_hint_width())
        if spec.preset_keys and current == PRESET_CUSTOM:
            return _fit_value_hint(
                current, preset_custom_hint(self.app, name, scope="global"),
                self._modes_hint_width())
        val = spec.values[0]
        return _fit_value_hint(val.value, val.hint, self._modes_hint_width())

    def _refresh_modes_hint(self) -> None:
        """Carry-fix (тот же приём, что и `_refresh_menu_rows`): досчитать усечение
        hint-строки ПОСЛЕ layout-прохода, когда регион уже известен."""
        if self._view != _MODES:
            return
        widgets = list(self.query(".set-modes-hint"))
        if widgets:
            widgets[-1].update(self._modes_hint_text())

    def _refresh_modes_rows(self) -> None:
        """Carry-fix (тот же приём, что и `_refresh_modes_hint`/`_refresh_menu_rows`):
        досчитать all-or-nothing усечение хвоста «· в текущей сессии: …» (§6.2) ПОСЛЕ
        layout-прохода, когда регион строки уже известен. Последние `len(names)`
        матчей по классу — свежесмонтированный комплект (то же соглашение
        «последний = свежий», что у `_modes_row_width`/`_modes_hint_width`/
        `_refresh_modes_hint`: в транзиентном двойном комплекте старые строки
        смонтированы раньше и ещё не отсоединены, новые — смонтированы позже и
        идут последними)."""
        if self._view != _MODES:
            return
        from tui.toggles import group_toggle_names
        names = group_toggle_names("rigor")
        widgets = list(self.query(".set-row"))
        for i, row in enumerate(widgets[-len(names):]):
            row.update(self._mode_row_text(names[i], i == self._modes_idx))

    def _modes_apply(self, direction: int) -> None:
        """←/→/Enter на строке тоггла (канон §1/§3): мгновенно записать
        переключённое ГЛОБАЛЬНОЕ значение в config.yml (Settings = глобальный
        дефолт, дизайн 2026-07-21 §3). Цикл значения — от ТЕКУЩЕГО глобального
        состояния. Guard записи — как у остальных секций (_writable): пакетный
        шаблон не пишем. Ошибка — флеш, значение не меняется. Сессионные
        переопределения НЕ трогаем — их перекрытие показывает пометка §6.2."""
        import onboarding_config_writer as w
        from config import _default_config_path, validate_config_text
        from tui.toggles import cycle_value, TOGGLES, toggle_state_base, PRESETS, group_toggle_names
        names = group_toggle_names("rigor")
        name = names[self._modes_idx]
        spec = TOGGLES[name]
        current = toggle_state_base(self.app, name)
        new_value = cycle_value(name, current, direction)   # пресет: по low/medium/high
        if not self._writable():
            self._notice("✗ не сохранено: конфиг недоступен для записи — используется "
                         "пакетный шаблон (настройте GC_HOME или запустите gc-ua --init-config)")
            return
        values = PRESETS[new_value] if spec.preset_keys else {spec.dotted: new_value}
        try:
            w.write_values(
                self._config_path, values,
                template_path=_default_config_path(),
                validate=validate_config_text,
            )
        except (ValueError, OSError) as exc:
            self._notice(f"✗ не сохранено: {exc}")
            return
        # Хвост записи — как у соседних секций (_do_save): перечитать сырые значения
        # файла и применить через инъектированный _on_apply, а не звать reload мимо него.
        self._raw = self._read_raw()
        try:
            self._on_apply()
            self._config = getattr(self.app, "_config", None) or self._config
        except Exception as exc:
            self._notice(f"сохранено, но конфиг не перечитан: {exc}")
            return
        self._render_view()

    # --- секция «Профили» (витрина обоих корней + единое правило записи) ---
    def _profiles_base_path(self):
        from pathlib import Path
        from config import _default_config_path
        return Path(self._config_path or _default_config_path())

    def _profiles_rows(self) -> list[dict]:
        """Строки секции: одна на каждое найденное имя профиля (тёзки — ОДНОЙ
        строкой, побеждает проектный) + редактируемая строка «уровень по
        умолчанию» (←/→/Enter) — она всегда последняя и всегда есть, даже без
        единого профиля."""
        import os
        from pathlib import Path
        from config_modules.profiles import discover_all_refs, profile_roots
        roots = profile_roots(Path(os.getcwd()), self._profiles_base_path())
        grouped = discover_all_refs(roots)
        rows = []
        for name in sorted(grouped):
            refs = grouped[name]
            rows.append({
                "kind": "profile", "name": name, "ref": refs[0],
                "shadow": refs[1] if len(refs) > 1 else None,
            })
        if rows:
            # «— без профиля» — первой строкой: глобальная деактивация; в пустом
            # состоянии (профилей нет) строка бессмысленна — деактивировать нечего.
            rows.insert(0, {"kind": "none"})
        # «+ добавить профиль» — последней строкой списка (перенос паттерна
        # «Моделей», ревизия владельца): Enter = клавиша n. Есть и в пустом
        # состоянии — там это единственная строка-действие над строкой уровня.
        rows.append({"kind": "add"})
        rows.append({"kind": "level"})
        return rows

    def _profiles_on_level_row(self) -> bool:
        """Курсор секции «Профили» — на строке уровня (последняя, всегда есть):
        футер тогда обещает ←/→/Enter вместо Enter-активации (позиционный
        футер, тот же приём, что у `_CONNECTION`)."""
        rows = self._profiles_rows()
        return bool(rows) and self._profiles_idx == len(rows) - 1

    def _profiles_on_add_row(self) -> bool:
        """Курсор — на строке «+ добавить профиль…» (предпоследняя): у неё свой
        футер, клавиши объектов на ней не работают."""
        rows = self._profiles_rows()
        return (0 <= self._profiles_idx < len(rows)
                and rows[self._profiles_idx]["kind"] == "add")
    def _profiles_on_none_row(self) -> bool:
        """Курсор — на строке «— без профиля». У неё свой глагол (снять выбор) и
        своё пустое состояние (снимать нечего), поэтому футер тоже свой."""
        rows = self._profiles_rows()
        if not rows:
            return False
        idx = min(self._profiles_idx, len(rows) - 1)
        return rows[idx]["kind"] == "none"

    def _project_config_candidate_path(self):
        from config_modules.env_vars import project_gc_dir
        return project_gc_dir() / "config.yml"

    def _profile_summary(self, path):
        """Сводка файла профиля через кэш прохода (`_profiles_disk_cache`).

        Чтение + разбор YAML — самая дорогая часть отрисовки секции: строки
        спрашивают минимум четыре независимых места за кадр (футер, монтирование,
        refresh строк, refresh подсказки), и без кэша каждый файл парсился заново
        на каждом из них. Кэш живёт один проход UI — сбрасывается на входе в
        `_render_view` и на обработке клавиши, поэтому правка профиля извне
        по-прежнему видна с первого же нажатия."""
        from config_modules.profiles import profile_display_summary
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
            import config_raw
            from config import _project_local_config_path
            project_path = _project_local_config_path()
            cache["project_doc"] = (
                config_raw.read_document(project_path) if project_path is not None else {}
            )
        return cache["project_doc"]

    def _project_config_sets(self, key: str) -> bool:
        """Проектный `.gc/config.yml` уже несёт топ-уровневый ключ `key` (любое
        значение, даже пустую строку — deep-merge отдаёт победу оверлею по факту
        наличия ключа, не по его «истинности») — тогда запись ЭТОГО ключа в базовый
        конфиг была бы молчаливым no-op: проектный слой всё равно перекроет её."""
        return key in self._project_config_doc()

    def _project_sets_profile_pointer(self) -> bool:
        return self._project_config_sets("profile")

    def _profile_activation_target(self, ref):
        """Единое правило записи: проектный профиль → проектный конфиг (создаётся
        при отсутствии); профиль «рядом с базой» → базовый конфиг запуска, ЕСЛИ
        проектный конфиг уже не задаёт указатель сам — иначе тоже проектный (иначе
        активация была бы молчаливым no-op: проектный слой всё равно победит).
        Никогда не пишет указатель на проектный профиль в базовый конфиг."""
        if ref.root_kind == "project" or self._project_sets_profile_pointer():
            return self._project_config_candidate_path()
        return self._profiles_base_path()

    def _write_profile_pointer(self, path, name: str) -> None:
        """Запись `profile: "<name>"`: якорь гарантируется `ensure_pointer_anchor`
        (топ-уровневый `profile:` из пакетного шаблона, идемпотентно), значение —
        существующим `write_values`. Файл, которого ещё нет (проектный
        `.gc/config.yml`), создаёт отдельная простая функция — `write_values` по
        контракту требует существующего файла."""
        from pathlib import Path
        import onboarding_config_writer as ocw
        from config import _default_config_path, validate_config_text
        path = Path(path)
        if not path.is_file():
            ocw.create_pointer_file(path, name)
            return
        ocw.ensure_pointer_anchor(path, template_path=_default_config_path())
        missing = ocw.write_values(path, {"profile": name}, validate=validate_config_text)
        if missing:
            raise ValueError(f"не удалось записать: {', '.join(missing)}")

    def _deactivation_target(self):
        """Куда пишется `profile: ""` при деактивации — то же единое правило, что
        у активации: слой, где запись возымеет эффект. Проектный конфиг задаёт
        указатель → туда (запись в базовый была бы молчаливым no-op); иначе —
        базовый конфиг запуска."""
        if self._project_sets_profile_pointer():
            return self._project_config_candidate_path()
        return self._profiles_base_path()

    def _deactivate_profile(self) -> None:
        """Space на «— без профиля» (клавиша перевешена решением В1 волны B):
        деактивация записью `profile: ""` по единому правилу записи; «ничего не
        активно» — честный no-op."""
        config_active = self._config.get("profile") or None
        if config_active is None:
            self._title_note("профиль не выбран")
            return
        target = self._deactivation_target()
        if not self._target_writable(target):
            self._notice(self._TEMPLATE_WRITE_REFUSAL)
            return
        try:
            self._write_profile_pointer(target, "")
        except (ValueError, OSError) as exc:
            self._notice(f"✗ не сохранено: {exc}")
            return
        try:
            self._on_apply()
            self._config = getattr(self.app, "_config", None) or self._config
        except Exception as exc:
            self._notice(f"записано в {target}, но конфиг не перечитан: {exc}")
            return
        self._raw = self._read_raw()
        self._origins = self._read_origins()
        session_overrides = getattr(self.app, "_session_overrides", None) or {}
        session_name = session_overrides.get("profile")
        if session_name:
            effect = f'текущая сессия остаётся на "{session_name}" (сессионный выбор /profiles)'
        else:
            effect = "применится со следующего хода агента"
        self._render_view()
        self._flash(f"Профиль деактивирован — {target}. {effect}")

    def _activate_profile(self, name: str, ref) -> None:
        """Space на строке профиля (клавиша перевешена решением В1 волны B). «Уже
        активен» — сравнение с КОНФИГ-указателем (`self._config["profile"]`, то, что
        реально пишет активация), не с сессионным выбором: сессия — отдельный
        механизм (`/profiles`), запись файла её не трогает."""
        config_active = self._config.get("profile") or None
        if name == config_active:
            self._title_note("уже активен")
            return
        target = self._profile_activation_target(ref)
        if not self._target_writable(target):
            self._notice(self._TEMPLATE_WRITE_REFUSAL)
            return
        try:
            self._write_profile_pointer(target, name)
        except (ValueError, OSError) as exc:
            self._notice(f"✗ не сохранено: {exc}")
            return
        try:
            self._on_apply()
            self._config = getattr(self.app, "_config", None) or self._config
        except Exception as exc:
            self._notice(f"записано в {target}, но конфиг не перечитан: {exc}")
            return
        self._raw = self._read_raw()
        self._origins = self._read_origins()
        session_overrides = getattr(self.app, "_session_overrides", None) or {}
        session_name = session_overrides.get("profile")
        if session_name and session_name != name:
            effect = f'текущая сессия остаётся на "{session_name}" (сессионный выбор /profiles)'
        else:
            effect = "применится со следующего хода агента"
        self._render_view()
        self._flash(f"Профиль «{name}» активирован — {target}. {effect}")

    def _level_write_target(self):
        """Куда пишется `level` — то же единое правило, что у активации/деактивации
        профиля рядом (`_deactivation_target`), но по СВОЕМУ ключу: проектный
        `.gc/config.yml` уже несёт `level:` → пишем туда (запись в базовый была бы
        перекрыта оверлеем проекта); иначе — базовый конфиг запуска. Указатель
        `profile:` проекта тут ни при чём — он перекрывает выбор профиля, а не
        уровень; прочие перекрытия (`level` внутри самого файла профиля) ловятся
        по факту — `_level_apply` сверяет действующее значение после перечитки."""
        if self._project_config_sets("level"):
            return self._project_config_candidate_path()
        return self._profiles_base_path()

    def _level_apply(self, direction: int) -> None:
        """←/→/Enter на строке уровня (канон «Режимов работы» — `_modes_apply`):
        переключить уровень по кругу от ТЕКУЩЕГО значения (`TOGGLES["level"].values`
        — единый источник, будущие значения подхватятся сами) и записать в файл
        (`_level_write_target` — то же правило, что у активации профиля рядом).
        `self._config["level"]` выставляется сразу — строка/подсказка/футер
        отражают новое значение без ожидания реального reload."""
        import onboarding_config_writer as w
        from config import _default_config_path, validate_config_text
        from tui.toggles import cycle_value
        current = str(self._config.get("level") or "default")
        new_value = cycle_value("level", current, direction)
        target = self._level_write_target()
        if not self._target_writable(target):
            self._notice(self._TEMPLATE_WRITE_REFUSAL)
            return
        try:
            # Якорь `level:` — как у указателя профиля рядом (`_write_profile_pointer`):
            # в конфигах, созданных до появления ключа, строки нет, а сам write_values
            # топ-уровневый скаляр не дописывает.
            w.ensure_pointer_anchor(target, template_path=_default_config_path(), key="level")
            missing = w.write_values(
                target, {"level": new_value},
                template_path=_default_config_path(),
                validate=validate_config_text,
            )
            if missing:
                raise ValueError(f"не удалось записать: {', '.join(missing)}")
        except (ValueError, OSError) as exc:
            self._notice(f"✗ не сохранено: {exc}")
            return
        self._config["level"] = new_value
        self._raw = self._read_raw()
        try:
            self._on_apply()
            fresh = getattr(self.app, "_config", None)
            if fresh is not None:
                self._config = fresh
        except Exception as exc:
            self._notice(f"записано в {target}, но конфиг не перечитан: {exc}")
            return
        self._render_view()
        # Записанное значение может быть перекрыто слоем выше (проектный конфиг,
        # файл профиля) — тогда честнее сказать, что действует, чем показывать
        # «установлен» и рисовать строку/футер по значению, которого нет.
        effective = str(self._config.get("level") or "default")
        if effective != new_value:
            self._notice(f"записано в {target}, но действует «{effective}» — "
                         "значение перекрыто слоем выше (проектный конфиг или профиль)")
            return
        self._flash(f"Уровень «{new_value}» установлен — {target}")

    # --- CRUD файлов профилей (волна B, Task 5) ---
    def _profiles_base_root(self):
        """Базовый корень профилей — единственная цель СОЗДАНИЯ (§12.3: проектный
        `.gc/profiles` остаётся ручной YAML-территорией, территорию не расширяем).
        Правило корней живёт в одном месте — `profile_roots` (проектный первым)."""
        import os
        from pathlib import Path
        from config_modules.profiles import profile_roots
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
        return next((r for r in self._profiles_rows()
                     if r["kind"] == "profile" and r["name"] == name), None)

    def _base_config_doc(self) -> dict:
        """Сырой документ базового config.yml через кэш прохода — симметрично
        `_project_config_doc`: указатель `profile:` живёт в обоих файлах, и
        спрашивают их одни и те же места."""
        cache = self._profiles_disk_cache
        if "base_doc" not in cache:
            import config_raw
            cache["base_doc"] = config_raw.read_document(self._profiles_base_path())
        return cache["base_doc"]

    def _profile_pointer_layers(self, name: str) -> list:
        """Файловые слои, чей указатель `profile:` называет `name` (проектный
        первым — порядок победы при merge).

        Спрашивать «активен ли профиль» у merged-конфига (`self._config["profile"]`)
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
        layers = []
        if self._project_config_doc().get("profile") == name:
            layers.append(self._project_config_candidate_path())
        if self._base_config_doc().get("profile") == name:
            layers.append(self._profiles_base_path())
        return layers

    def _repoint_profile_layers(self, layers: list, value: str) -> tuple[list, str | None]:
        """Записать `profile: <value>` в каждый слой. Возвращает (записанные,
        текст ошибки | None) — откат частичной записи на вызывающем: только он
        знает, каким глаголом назвать неслучившееся действие."""
        written = []
        for target in layers:
            try:
                self._write_profile_pointer(target, value)
            except (ValueError, OSError) as exc:
                return written, str(exc)
            written.append(target)
        return written, None

    def _profile_name_error(self, name: str, *, exclude: str | None = None) -> str | None:
        """Имя годится для создания/переименования — иначе текст ошибки. `exclude` —
        текущее имя при переименовании (сам себе тёзкой не считается).

        Разделители пути отклоняем отдельно от прочего: имя идёт прямо в имя файла
        (`<имя>.yml`), и это единственная защита от выхода за пределы корня."""
        from config_modules.model_registry import RESERVED_WORDS
        from config_modules.profiles import discover_all_refs, profile_roots
        import os
        from pathlib import Path
        if not name:
            return "введите имя профиля"
        if "/" in name or "\\" in name or name in (".", ".."):
            return "имя не должно содержать разделители пути"
        if name in RESERVED_WORDS:
            return f'имя «{name}» зарезервировано ({", ".join(RESERVED_WORDS)})'
        roots = profile_roots(Path(os.getcwd()), self._profiles_base_path())
        if name in discover_all_refs(roots) and name != exclude:
            return f'профиль «{name}» уже существует'
        return None

    def _profile_write_block(self, *, ref=None, name: str | None = None) -> str | None:
        """Что мешает файловой операции над профилем — причина отказа либо None.

        Предикат ОДИН на дверь (`n`/`r`/`Del`) и на саму операцию: разойдясь, они
        дают ложное обещание — поле ввода/баннер поднимаются лишь затем, чтобы
        отказать после ответа. Проверяются оба объекта, и они разные: каталог
        профилей (туда ляжет файл) и файл конфига, чей указатель придётся
        переписать (`_profile_pointer_layers`). Второй бывает непишущимся при
        писучем первом — проектный профиль, названный базовым конфигом."""
        if not self._profiles_root_writable(ref):
            return "каталог профилей недоступен для записи"
        for target in (self._profile_pointer_layers(name) if name else []):
            if not self._target_writable(target):
                return f"файл конфига {target} недоступен для записи"
        return None

    def _profile_write_refusal(self, done: str, reason: str) -> str:
        return self._PROFILE_FILE_WRITE_REFUSAL.format(done=done, reason=reason)

    def _begin_profile_name_prompt(self, mode: str, original: str | None = None) -> None:
        """Поднять инлайн-ввод имени (контракт п.5): поле раскрывается на месте —
        под своей строкой при переименовании, под списком при создании."""
        ref = None if original is None else (self._profile_row_by_name(original) or {}).get("ref")
        block = self._profile_write_block(ref=ref, name=original)
        if block:
            self._notice(self._profile_write_refusal(
                "переименовано" if mode == "rename" else "создано", block))
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
        не был активен: сессионный выбор (`/profiles`) мог указывать именно на этот
        файл, и санацию (§9.2) делает именно она (`app.reload_config` →
        `sanitize_session_profile` + notice). False — перечитать не удалось, о чём
        уже сказано notice'ом (сама операция при этом состоялась)."""
        try:
            self._on_apply()
            self._config = getattr(self.app, "_config", None) or self._config
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
        import onboarding_config_writer as ocw
        from config_modules.profiles import PROFILE_SKELETON
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
        row = next((r for r in rows if r["kind"] == "profile" and r["name"] == name), None)
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
        return (getattr(self.app, "_session_overrides", None) or {}).get("profile")

    def _profile_session_effect(self, before: str | None, *, pointer_changed: bool) -> str:
        """Эффект операции для ТЕКУЩЕЙ сессии — в плашку, а не только в `/notices`
        (канон волны A, `_activate_profile`). Считается ПОСЛЕ перечитки: снял ли
        сессионный выбор санатор (§9.2) и куда он переехал, видно только по факту.

        `pointer_changed` — переписан ли хоть один действующий указатель. Без него
        хвост «применится со следующего хода агента» вставал бы и там, где для
        агента не изменилось НИЧЕГО (переименован профиль, на который не ссылается
        ни один слой), — тот же класс лжи, за который переписывались футеры. Нечего
        сказать — пустая строка, а не вежливая неправда."""
        after = self._session_profile_name()
        if after and after != before:
            return f'сессионный выбор /profiles переехал на «{after}»'
        if after:
            return f'текущая сессия остаётся на «{after}» (сессионный выбор /profiles)'
        if before:
            acting = (self._config.get("profile") or None) or "без профиля"
            return f'сессионный выбор «{before}» снят — действует «{acting}»'
        return "применится со следующего хода агента" if pointer_changed else ""

    def _rename_profile(self, original: str, new_name: str) -> None:
        """`r` → новое имя: переименовываются файл И указатель в КАЖДОМ файловом
        слое, который называет старое имя (`_profile_pointer_layers`).

        Порядок: указатели ПЕРЕД файловой операцией. Обратный оставлял бы висячий
        `profile: <старое имя>` без файла при сбое записи указателя ПОСЛЕ уже
        состоявшегося rename, а холодный старт с нерезолвимым указателем — строгая
        ошибка (§3.1)."""
        if new_name == original:
            self._cancel_profile_name_prompt()      # ничего не изменилось — тихая отмена
            return
        error = self._profile_name_error(new_name, exclude=original)
        if error:
            self._notice(f"✗ не переименовано: {error}")
            return
        row = self._profile_row_by_name(original)
        if row is None:
            self._cancel_profile_name_prompt()      # файл исчез под ногами — показать как есть
            return
        ref = row["ref"]
        block = self._profile_write_block(ref=ref, name=original)
        if block:
            self._notice(self._profile_write_refusal("переименовано", block))
            return
        layers = self._profile_pointer_layers(original)
        session_before = self._session_profile_name()
        written, error = self._repoint_profile_layers(layers, new_name)
        if error is not None:
            self._rollback_profile_pointers(written, original, file_exc=error,
                                            action="переименовать")
            return
        try:
            ref.path.rename(ref.path.with_name(f"{new_name}.yml"))
        except OSError as exc:
            self._rollback_profile_pointers(written, original, file_exc=exc,
                                            action="переименовать")
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
                pass        # meta недоступна: оверрайд откачен, дальше его снимет санация
        if not self._reload_after_profile_file_change("переименовано"):
            return
        self._render_view()
        self._flash(" ".join(filter(None, [
            f"Профиль «{original}» переименован в «{new_name}».",
            self._profile_session_effect(session_before, pointer_changed=bool(layers)),
        ])))

    def _request_delete_profile(self, name: str) -> None:
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
        parts = [f"Удалить профиль «{name}»? Файл уедет в backups/ рядом с ним."]
        layers = self._profile_pointer_layers(name)
        if layers:
            active = " (сейчас активен)" if (self._config.get("profile") or None) == name else ""
            parts.append(f"Указатель на него{active} снимется в: "
                         + ", ".join(str(t) for t in layers) + ".")
        if self._session_profile_name() == name:
            parts.append("Сессионный выбор /profiles тоже снимется.")
        parts.append("Enter — удалить · Esc — отмена")
        return " ".join(parts)

    def _confirm_delete_profile(self) -> None:
        name = self._profiles_delete_name
        self._profiles_delete_name = None
        if name is not None:
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
            self._render_view()                     # файл исчез под ногами — просто перерисовать
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
            self._rollback_profile_pointers(written, name, file_exc=error, action="удалить")
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
            self._rollback_profile_pointers(written, name, file_exc=exc, action="удалить")
            return
        if not self._reload_after_profile_file_change("удалено"):
            return
        self._profiles_idx = 0
        self._render_view()
        parts = [f"Профиль «{name}» удалён — бэкап: {backup}."]
        if shadow is not None:
            # Удалён победивший тёзка: строка с тем же именем остаётся в списке
            # (из второго корня) — молчание читалось бы как «удаление не сработало».
            parts.append(f"Имя «{name}» теперь резолвится в {shadow.path}.")
        parts.append(self._profile_session_effect(session_before,
                                                  pointer_changed=bool(layers)))
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

    def _rollback_profile_pointers(self, layers: list, old_value: str, *, file_exc,
                                   action: str) -> None:
        """Указатели уже переписаны, а файловая операция ПОСЛЕ них упала — вернуть
        каждый к тому, что действовало до начала операции. Иначе конфиг остаётся
        со ссылкой на несуществующее/непереименованное имя, а холодный старт с
        нерезолвимым `profile:` — строгая ошибка (§3.1)."""
        stuck = []
        for target in layers:
            try:
                self._write_profile_pointer(target, old_value)
            except (ValueError, OSError) as rollback_exc:
                stuck.append(f"{target} ({rollback_exc})")
        if stuck:
            self._notice(
                f"✗ не удалось {action}: {file_exc}; указатель НЕ откачен в "
                f"{', '.join(stuck)} — конфиг может ссылаться на несуществующий "
                "файл, поправьте вручную")
            return
        self._notice(f"✗ не удалось {action}: {file_exc}")

    def _profiles_name_width(self, rows: list[dict]) -> int:
        names = [r["name"] for r in rows if r["kind"] == "profile"]
        return max((len(n) for n in names), default=0) + 3

    def _profiles_row_width(self) -> int:
        """Ширина строки внутри рамки (padding 0 1 — минус 2) — carry-fix того же
        рода, что `_modes_row_width`: региона нет до layout-прохода при монтировании."""
        widgets = list(self.query(".set-profiles-row"))
        if not widgets:
            return 0
        return max(0, widgets[-1].region.width - 2)

    _LEVEL_HEAD = "level · уровень модели по умолчанию"

    def _level_columns(self) -> tuple[int, int, int, int]:
        """(label_w, base_w, extra_w, value_w) — тот же приём и те же формулы,
        что `_modes_columns` (Task 13: канон «Режимов работы» переносится
        строкой целиком, не по элементам — урок живого смока 2026-08-10). В
        этой секции строка одна, но паддинг всё равно считается формулой, а
        не подбирается на глаз."""
        from tui.toggles import TOGGLES
        spec = TOGGLES["level"]
        label_w = len(self._LEVEL_HEAD) + 3 + 2
        base_w = 2 * len(spec.values) - 1
        extra_w = 0
        value_w = max(len(v) for v in spec.names)
        return label_w, base_w, extra_w, value_w

    def _level_row_text(self, selected: bool) -> Text:
        """Task 13: грамматика строки — целиком по канону `_mode_row_text`
        (не по кусочкам, как переносили раньше): «имя ручки · описание» одним
        выровненным блоком INK -> канонный трек (Task 12, `padded_track`,
        tui/toggles.py) -> значение INK. «по умолчанию» в описании сохранено
        — отличает постоянное значение (пишется в файл) от сессионного
        `/level`. Число позиций трека — из `TOGGLES["level"].values` (единый
        источник, будущий авто-режим сделает трек четырёхпозиционным сам);
        цвет горящей позиции — тот же визуальный язык, что точка-индикатор
        футера (`main.py::_footer_level_color`)."""
        from tui.toggles import TOGGLES, padded_track
        spec = TOGGLES["level"]
        level = str(self._config.get("level") or "default")
        label_w, base_w, extra_w, value_w = self._level_columns()
        track = padded_track(spec, level, extra_lit=None, base_w=base_w, extra_w=extra_w)
        t = Text()
        t.append("▸ " if selected else "  ", style=ACCENT)
        t.append(self._LEVEL_HEAD.ljust(label_w - 2), style=INK)
        t.append_text(track)
        t.append("  ")
        t.append(level.ljust(value_w), style=INK)
        return t

    def _profile_row_text(self, row: dict, rows: list[dict], selected: bool) -> Text:
        name, ref = row["name"], row["ref"]
        config_active = self._config.get("profile") or None
        name_w = self._profiles_name_width(rows)
        t = Text()
        t.append("▸ " if selected else "  ", style=ACCENT)
        t.append(f"{name:<{name_w}}", style=INK)
        source = "проектный" if ref.root_kind == "project" else "глобальный"
        t.append(f"{source:<11}", style=DIM)
        active_marker = "● активен"
        if name == config_active:
            t.append(active_marker, style=OK)
            if self._project_sets_profile_pointer() and ref.root_kind != "project":
                t.append(" · выбран проектом", style=DEEP_DIM)
        else:
            t.append(" " * len(active_marker), style=DEEP_DIM)
        summary = self._profile_summary(ref.path)
        tail = ""
        if summary.slots:
            tail += "   " + " · ".join(f"{lvl} {alias}" for lvl, alias in summary.slots.items())
        if summary.overlay_sections:
            width = self._profiles_row_width()
            if width <= 0:
                tail += "".join(f"+{n}" for n in summary.overlay_sections)
            else:
                budget = max(0, width - len(t.plain) - len(tail))
                tail += fit_overlay_suffix(summary.overlay_sections, budget)
        if tail:
            t.append(tail, style=DIM)
        session_overrides = getattr(self.app, "_session_overrides", None) or {}
        session_name = session_overrides.get("profile")
        if session_name == name and session_name != config_active:
            t.append("   · выбран в текущей сессии", style=DEEP_DIM)
        return t

    def _add_profile_row_text(self, selected: bool) -> Text:
        """Строка-действие «+ добавить профиль…» — перенос паттерна «Моделей»:
        Enter = клавиша n, цвет и грамматика те же (DIM + многоточие — пробы
        владельца: DEEP_DIM читался подсказкой, тил перетягивал глаз)."""
        t = Text()
        t.append("▸ " if selected else "  ", style=ACCENT)
        t.append("+ добавить профиль…", style=DIM)
        return t

    def _none_row_text(self, rows: list[dict], selected: bool) -> Text:
        """Строка «— без профиля»: глобальная деактивация; `● активен` — когда
        конфиг-указатель пуст."""
        config_active = self._config.get("profile") or None
        name_w = self._profiles_name_width(rows)
        t = Text()
        t.append("▸ " if selected else "  ", style=ACCENT)
        label = "без профиля"
        t.append(label.ljust(max(name_w + 11, len(label) + 3)), style=INK)
        active_marker = "● активен"
        if config_active is None:
            t.append(active_marker, style=OK)
        else:
            t.append(" " * len(active_marker), style=DEEP_DIM)
        session_overrides = getattr(self.app, "_session_overrides", None) or {}
        if session_overrides.get("profile") == "" and config_active is not None:
            t.append("   · выбран в текущей сессии", style=DEEP_DIM)
        return t

    def _profiles_row_under_cursor(self, rows: list[dict]) -> dict:
        """Строка под курсором с клампом индекса: набор строк перечитывается с диска
        на каждый рендер и на каждое нажатие, а файл профиля мог исчезнуть под ногами
        между ними (правка/удаление извне) — курсор тогда указывает за конец."""
        self._profiles_idx = min(self._profiles_idx, len(rows) - 1)
        return rows[self._profiles_idx]

    def _profiles_hint_text(self, rows: list[dict]) -> str:
        row = self._profiles_row_under_cursor(rows)
        if row["kind"] == "add":
            # Полный путь — то же правило, что у «запись:» секции «Модели»: одно
            # имя не говорит, куда именно ляжет файл.
            return f"Enter — создать файл {self._profiles_base_root() / '<имя>.yml'}"
        if row["kind"] == "level":
            from tui.toggles import _LEVEL_ROLE_TEXT
            level = str(self._config.get("level") or "default")
            role = _LEVEL_ROLE_TEXT.get(level, "")
            head = f"{level} — {role}" if role else level
            target = self._level_write_target()
            lines = [head, f"←/→/Enter — переключить (запись: {target})",
                    "на сессию — /level"]
            return "\n".join(lines)
        if row["kind"] == "none":
            if (self._config.get("profile") or None) is None:
                return "профиль не выбран — действует чистый конфиг"
            return f"Space — деактивировать (запись: {self._deactivation_target()})"
        name, ref = row["name"], row["ref"]
        lines = [str(ref.path)]
        target = self._profile_activation_target(ref)
        lines.append(f"Space — активировать (запись: {target})")
        if row.get("shadow") is not None:
            lines.append(f"тёзка перекрыта: {row['shadow'].path}")
        if self._project_sets_profile_pointer() and (self._config.get("profile") or None) == name \
                and ref.root_kind != "project":
            lines.append("выбор задан .gc/config.yml проекта")
        summary = self._profile_summary(ref.path)
        if summary.error:
            lines.append(f"не удалось прочитать: {summary.error}")
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
            frame.mount(Static(
                "Профиль — отдельный файл-конфиг: переключаемый набор ЛЮБЫХ "
                "настроек — модели, уровни, гварды и любые другие секции конфига",
                classes="set-hint set-dyn"))
            frame.mount(Static(
                "Создать — клавишей n; вручную — сохраните файл "
                "~/.gc/profiles/<имя>.yml, например:",
                classes="set-hint set-dyn"))
            skeleton = (
                "models:\n"
                "  flash:\n"
                "    url: https://openrouter.ai/api/v1/chat/completions\n"
                "    token: sk-or-...\n"
                "slots:\n"
                "  default: flash\n"
                "  light: flash"
            )
            frame.mount(Static(skeleton, classes="set-hint set-dyn", markup=False))
        # Цикл идёт и в пустом состоянии: там из строк-нелевелов остаётся одна
        # «+ добавить профиль» — строка-действие под подсказкой-примером.
        for i, row in enumerate(rows):
            if row["kind"] == "level":
                continue
            selected = i == self._profiles_idx
            cls = "set-row set-dyn set-profiles-row" + (" selected" if selected else "")
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
        if profile_rows:
            # Черта перед строкой уровня: уровень — не «ещё один профиль»,
            # отделяем как группы «Режимов работы».
            frame.mount(Rule(classes="set-group-sep"))
        level_idx = len(rows) - 1
        selected = level_idx == self._profiles_idx
        # Плотность B: при живом списке воздух строке уровня даёт черта над ней
        # (margin-top .set-group-sep), свой класс не нужен. В пустом состоянии
        # черты нет — set-spaced-gap даёт воздух сверху и снизу (не пара
        # раздельных классов: longhand-margins в Textual перетирают друг друга).
        spacing = "" if profile_rows else "set-spaced-gap "
        cls = f"set-row set-dyn {spacing}set-profiles-row" + (" selected" if selected else "")
        frame.mount(Static(self._level_row_text(selected), classes=cls))
        frame.mount(Static(self._profiles_hint_text(rows),
                           classes="set-hint set-dyn set-profiles-hint"))
        if self._profiles_delete_name is not None:
            frame.mount(Static(Text(self._profile_delete_confirm_text(), style=WARN),
                               classes=f"set-confirm set-dyn {KEEP_IN_VIEW}", markup=False))

    def _name_prompt_belongs_to(self, row: dict) -> bool:
        """Строка, под которой раскрывается инлайн-ввод имени: своя строка профиля
        при переименовании (создание строки не имеет — см. `_mount_profiles_section`)."""
        prompt = self._profiles_name_prompt
        return (prompt is not None and prompt["mode"] == "rename"
                and row["kind"] == "profile" and row["name"] == prompt["original"])

    def _mount_profile_name_prompt(self, frame: Vertical) -> None:
        """Инлайн-ввод имени (контракт п.5): поле без отдельной подписи — контекст
        даёт строка, под которой оно раскрылось; рамка — канон полей /settings."""
        from tui.widgets.paste_input import PasteInput
        prompt = self._profiles_name_prompt
        prefill = prompt["original"] if prompt["mode"] == "rename" else ""
        inp = PasteInput(value=prefill or "", placeholder="имя профиля",
                         id="set-profile-name",
                         classes=f"set-dyn set-spaced set-framed {KEEP_IN_VIEW}")
        frame.mount(inp)
        inp.focus()

    def _refresh_profiles_rows(self) -> None:
        """Carry-fix (тот же приём, что `_refresh_modes_rows`): досчитать усечение
        суффикса секций-оверлеев ПОСЛЕ layout-прохода, когда регион уже известен."""
        if self._view != _PROFILES:
            return
        rows = self._profiles_rows()
        widgets = list(self.query(".set-profiles-row"))
        if not widgets:
            return
        fresh = widgets[-len(rows):]
        for i, row in enumerate(rows):
            selected = i == self._profiles_idx
            if row["kind"] == "level":
                fresh[i].update(self._level_row_text(selected))
            elif row["kind"] == "none":
                fresh[i].update(self._none_row_text(rows, selected))
            elif row["kind"] == "add":
                fresh[i].update(self._add_profile_row_text(selected))
            else:
                fresh[i].update(self._profile_row_text(row, rows, selected))

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
            self._notice(f"✗ не открыто: не удалось прочитать файл профиля: {error}")
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
        from tui.editors.models_section import ModelsSectionEditor
        for text, cls in self._profile_overlay_specs():
            frame.mount(Static(text, classes=cls, markup=False))
        endpoint, auth, verify, _m, proxy = self._probe_args_from_config()
        from config_modules.endpoint_presets import endpoint_presets_from_config
        frame.mount(ModelsSectionEditor(
            target=row["ref"].path, template="", is_profile=True,
            base_registry=self._base_registry_snapshot,
            effective_url=endpoint, effective_auth=auth, verify=verify,
            config_proxy=proxy,
            presets=endpoint_presets_from_config(self._config or {}),
            active_level=str((self._config or {}).get("level") or "default"),
            on_change=self._models_changed, on_notice=self._models_notice))
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
        if event.key not in self._VIEW_FILE_KEYS or isinstance(self.focused, Input):
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
        from tui.screens.file_view import FileViewerScreen
        from tui.services.file_view import ViewPositionStore
        store = getattr(self.app, "view_positions", None)
        if store is None:
            store = ViewPositionStore()
            self.app.view_positions = store
        path = row["ref"].path
        self.app.push_screen(FileViewerScreen(
            raw_path=str(path), cwd=str(path.parent), store=store,
            settings=self._file_view_settings()))

    def _base_registry_snapshot(self) -> dict:
        """Тень основного конфига для под-вида профиля: что действует в строках
        реестра и уровней, пока профиль своего не говорит (§3 спеки эффективного
        вида). `{"models", "slots", "assign", "base_url"}`.

        Источник — ДОКУМЕНТ базового config.yml (цель основной секции), а не
        merged `self._config`: тот собран с УЖЕ АКТИВНЫМ профилем, и тень стала бы
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
        import config_raw
        from config_modules.env_interpolation import resolve_env_placeholders
        from onboarding import URL_SENTINEL
        if not self._config_path:
            return {}
        document = config_raw.read_document(self._config_path)
        sections = resolve_env_placeholders(
            {key: document.get(key)
             for key in ("models", "slots", "assign", "backend")})
        backend = sections.get("backend")
        backend = backend if isinstance(backend, dict) else {}
        url = backend.get("completions_url") or ""
        return {
            "models": sections.get("models") or {},
            "slots": sections.get("slots") or {},
            "assign": sections.get("assign") or {},
            "base_url": "" if url == URL_SENTINEL else url,
        }

    def _profile_document(self, path) -> dict:
        """Сырой документ профиля через кэш прохода — сосед `_profile_summary`:
        витрине под-вида нужны не только оверлеи, но и то, ЧТО из секций моделей
        лежит в файле (пустой реестр меняет состав форм, а с ним и состав
        «невидимого»)."""
        cache = self._profiles_disk_cache.setdefault("profile_docs", {})
        key = str(path)
        if key not in cache:
            import config_raw
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
        входит: строка «уровень модели по умолчанию» секции «Профили» правит
        ГЛОБАЛЬНЫЙ `level:` конфига, а не ключ внутри файла профиля, и без превью
        профиль с `level: max` не проявлялся бы на экране нигде (ruling координатора
        2026-08-11). В СТРОКЕ СПИСКА исключение остаётся — там канон §12.3 со своей
        колонкой состава (`profile_display_summary` не тронут)."""
        from tui.editors.models_section import effective_registry_nonempty
        if effective_registry_nonempty(document, self._base_registry_snapshot()):
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
        return sorted(key for key, value in document.items()
                      if key not in covered and not _empty_anchor(value))

    def _profile_overlay_width(self) -> int:
        """Ширина строки превью внутри рамки (padding 0 1 — минус 2); до layout-прохода
        региона нет — 0, что трактуется как «без усечения» (carry-fix, как у
        `_profiles_row_width`)."""
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
        budget = 0 if width <= 0 else max(0, width - len(self._PROFILE_OVERLAY_HEAD))
        return self._PROFILE_OVERLAY_HEAD + fit_mode_pairs(names, budget)

    def _profile_overlay_hint(self) -> str:
        """Подсказка под превью: список ЦЕЛИКОМ (усечённая строка выше могла его не
        вместить — та же пара «строка + hint», что у профиля в списке) плюс главный
        факт о них: форм для этих секций здесь нет, это YAML-территория (§12.4).

        Она же называет ход к ним (§11a): «правятся в YAML» без способа их увидеть
        оставляло бы человека вовсе без хода. Обещание гаснет вместе с самим
        просмотрщиком — теми же словами и по тому же гейту, что и в футере."""
        hint = (self._PROFILE_OVERLAY_HINT_HEAD
                + ", ".join(self._profile_overlay_names())
                + self._PROFILE_OVERLAY_HINT_TAIL)
        if not self._file_view_settings().enabled:
            return hint
        from tui.editors.models_section import _VIEW_KEY
        return f"{hint} · {_VIEW_KEY}"

    def _profile_overlay_specs(self) -> list[tuple[str, str]]:
        """(текст, css-класс) строк превью — пусто, когда переопределять нечего:
        профиль из одних моделей молчит, а не показывает пустую фразу."""
        if not self._profile_overlay_names():
            return []
        return [(self._profile_overlay_text(), "set-info set-dyn set-profiles-overlay"),
                (self._profile_overlay_hint(), "set-hint set-dyn set-profiles-overlay-hint")]

    def _refresh_profile_overlay(self) -> None:
        """Досчитать усечение ПОСЛЕ layout-прохода, когда регион строки уже известен
        (тот же carry-fix, что `_refresh_profiles_rows`)."""
        for widget in self.query(".set-profiles-overlay"):
            widget.update(self._profile_overlay_text())

    _UPDATE_ACTION_UPDATE = "Обновить агента"
    _UPDATE_ACTION_CHECK = "Проверить обновления"
    _UPDATE_ACTION_SYNC = "Обновить config.yml до текущей версии"
    _UPDATE_ACTION_RESET = "Сбросить конфиг к умолчанию"

    def _update_confirm_text(self) -> str:
        """I4: «в отдельном окне» правда только на Windows — на остальных платформах
        установка идёт в фоне без видимого окна (см. updater.runner.install_location_phrase).

        Если идёт ход агента — предупреждаем явно: закрытие прервёт выполнение (диалог
        сохранится в тред, но текущий ход не доведётся)."""
        from updater.runner import install_location_phrase
        runner = getattr(self.app, "active_runner", None)
        running = runner is not None and runner.is_running()
        head = ("Идёт выполнение задачи — обновление прервёт его и закроет агента"
                if running else "Агент закроется")
        return (f"{head}, установка пойдёт {install_location_phrase()}; когда "
                "завершится — запустите gc-ua заново. Текущий диалог сохранится "
                "в тред. Enter — обновить · Esc — отмена")

    def _mount_update_section(self, frame: Vertical) -> None:
        import app_version
        from updater.source import resolved_source
        src = app_version.install_source()
        dev = app_version.is_dev_install()   # признак один на все экраны
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
            frame.mount(Static(self._checked_line_text(), classes="set-info set-dyn"))
        actions = [self._UPDATE_ACTION_SYNC, self._UPDATE_ACTION_RESET] if dev else [
            self._UPDATE_ACTION_UPDATE, self._UPDATE_ACTION_CHECK,
            self._UPDATE_ACTION_SYNC, self._UPDATE_ACTION_RESET]
        self._update_actions = actions
        if self._update_idx is None or self._update_idx >= len(actions):
            self._update_idx = 0     # дефолт — верхнее действие (главный смысл секции)
        update_available = (self._update_check_result is not None
                            and self._update_check_result.status == "update")
        for i, label in enumerate(actions):
            selected = i == self._update_idx
            row = Text()
            row.append("▸ " if selected else "  ", style=ACCENT)
            # доступное обновление — хорошая новость → accent (teal), не алярм-warn
            row.append(label, style=ACCENT if (i == 0 and update_available) else INK)
            # первая строка-действие — один класс с top+bottom margin (раздельные
            # .set-gap+.set-spaced Textual перетирает → пропадал воздух под первым действием)
            spacing = "set-spaced-gap" if i == 0 else "set-spaced"
            cls = f"set-row set-dyn {spacing}" + (" selected" if selected else "")
            frame.mount(Static(row, classes=cls))
        if self._update_awaiting_confirm:
            frame.mount(Static(Text(self._update_confirm_text(), style=WARN),
                               classes=f"set-confirm set-dyn {KEEP_IN_VIEW}"))
        elif self._reset_awaiting_confirm:
            frame.mount(Static(Text(self._reset_confirm_text(), style=WARN),
                               classes=f"set-confirm set-dyn {KEEP_IN_VIEW}"))
        else:
            frame.mount(Static(
                self._update_hint_text(actions[self._update_idx], resolved),
                classes="set-hint set-dyn"))

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
            from updater.check import extra_args
            from updater.runner import build_install_command, install_location_phrase
            # [0] — абсолютный путь к uv (нужен detached-скрипту); в подсказке для юзера
            # показываем короткое «uv», как он сам набрал бы в терминале.
            cmd = " ".join(["uv"] + build_install_command(resolved, extra_args(self._config))[1:])
            return (f"Закроет агента, установка пойдёт {install_location_phrase()}; когда завершится — "
                    "запустите gc-ua заново. MCP и config.yml предложит обновить при "
                    "следующем запуске.\n"
                    f"То же из терминала: {cmd}")
        if action == self._UPDATE_ACTION_CHECK:
            return (f"Спросит у источника установки ({resolved.url} · {resolved.ref or 'master'}), "
                    "появились ли новые версии. Ничего не устанавливает.")
        if action == self._UPDATE_ACTION_SYNC:
            return ("Перепишет файл по образцу текущей версии, ваши значения сохранятся, "
                    "старый файл — в бэкап. На работу агента не влияет.")
        return ("Заменит config.yml пакетным умолчанием текущей версии — ваши правки уйдут "
                "в бэкап. Файлы профилей и настройки сессии останутся, но выбор профиля "
                "в config.yml сбросится.")

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
        import app_version
        from updater.source import resolved_source
        src = app_version.install_source()
        if src is None or src.editable:
            return
        resolved = resolved_source(_source=lambda: src)
        if resolved is None:
            return
        self._update_checking = True
        self._render_view()

        def _work() -> None:
            import updater.check as check_mod
            try:
                result = check_mod.check_for_update(resolved)
            except Exception as exc:
                result = check_mod.CheckResult("unknown", None, f"не удалось: {exc}")
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
        self.app._update_check_result = r     # вердикт сессии — общий (хаб, повторный вход в секцию)
        if r.status == "update":
            self._update_idx = 0        # «Обновить агента» первым, см. §5
        import time
        import app_version
        import upgrade_gate
        from updater.check import record_check_result, update_available_from_state
        src = app_version.install_source()
        installed = src.commit if src else None
        record_check_result(time.time(), r.remote_commit, installed)
        self.app._update_available = update_available_from_state(upgrade_gate.read_state(), installed)
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
        import app_version
        from updater.check import extra_args
        from updater.runner import build_install_command, install_location_phrase, spawn_detached_update
        from updater.source import resolved_source
        src = app_version.install_source()
        resolved = resolved_source(_source=lambda: src)
        if resolved is None:
            self._update_awaiting_confirm = False
            self._render_view()
            return
        command = build_install_command(resolved, extra_args(self._config))
        # Без авто-рестарта — единообразно с `gc-cli --update`: фоновая переустановка,
        # пользователь запускает gc-ua заново (venv нельзя заменить под живым процессом).
        spawn_detached_update(command, parent_pid=os.getpid(), restart_cmd=None)
        self.app.exit(message=f"Установка запущена {install_location_phrase()}. "
                              "Когда завершится — запустите gc-ua заново.")

    def _cancel_update(self) -> None:
        self._update_awaiting_confirm = False
        self._render_view()

    def _run_sync(self) -> None:
        import config_sync
        if not self._writable():
            self._notice("✗ пакетный шаблон не обновляется")
            return
        from pathlib import Path
        from config import _default_config_path
        try:
            user_text = Path(self._config_path).read_text(encoding="utf-8")
            template_text = Path(_default_config_path()).read_text(encoding="utf-8")
            if not config_sync.needs_sync(user_text, template_text):
                self._notice("config.yml уже актуален — обновлять нечего")
                return
        except (OSError, ValueError):
            pass                                                  # не смогли решить — пусть sync решит
        try:
            report = config_sync.sync_config(self._config_path)
        except (ValueError, OSError) as exc:
            self._notice(f"✗ config.yml НЕ обновлён: {exc}")
            return
        self._raw = self._read_raw()        # файл переписан по шаблону — перечитать
        lines = []
        try:
            self._on_apply()
            self._config = getattr(self.app, "_config", None) or self._config
        except Exception as exc:
            lines.append(f"сохранено, но конфиг не перечитан: {exc}")
        lines.append(f"✓ config.yml обновлён · перенесено: {report.transferred} · переименовано: {report.renamed}")
        if report.unknown:
            lines.append(f"неизвестных ключей: {', '.join(report.unknown)} (в бэкапе)")
        if report.reenter:
            lines.append(f"задайте заново: {', '.join(report.reenter)}")
        lines.append(f"бэкап: {report.backup_path}")
        self._report("\n".join(lines))

    def _reset_confirm_text(self) -> str:
        return ("config.yml будет заменён пакетным умолчанием текущей версии, ваш файл — "
                "в бэкап. Файлы профилей и настройки сессии останутся, но выбор профиля "
                "в config.yml сбросится. Enter — сбросить · Esc — отмена")

    def _request_reset(self) -> None:
        """Enter на «Сбросить конфиг к умолчанию»: confirm-баннер (канон §5)."""
        if self._reset_awaiting_confirm:
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
        if not self._writable():
            self._render_view()
            self._notice("✗ пакетный шаблон не сбрасывается")
            return
        import config_sync
        try:
            report = config_sync.reset_config(self._config_path)
        except (ValueError, OSError) as exc:
            self._render_view()
            self._notice(f"✗ config.yml НЕ сброшен: {exc}")
            return
        self._raw = self._read_raw()          # файл заменён шаблоном — перечитать
        self._origins = self._read_origins()
        lines = [f"✓ config.yml сброшен к умолчанию · бэкап: {report.backup_path}"]
        try:
            self._on_apply()
            self._config = getattr(self.app, "_config", None) or self._config
        except Exception as exc:
            lines.append(f"сброшено, но конфиг не перечитан: {exc}")
        session_overrides = getattr(self.app, "_session_overrides", None) or {}
        session_profile = session_overrides.get("profile")
        if session_profile:
            lines.append(f'сессия остаётся на профиле "{session_profile}" (/profiles) — сброс базы это не меняет')
        import onboarding
        if onboarding.is_unconfigured(self._config or {}):
            lines.append("настройка не завершена — откройте помощник (--init-config) или впишите поля вручную")
        self._render_view()
        self._report("\n".join(lines))

    # --- MCP-секция (спека C) ---
    def _mcp_editor(self):
        from tui.editors.mcp_section import McpSectionEditor
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
            self._notice(", ".join(f"{n} {'✓' if ok else '✗'}" for n, ok in results))

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
        slot = Vertical(Static(text, id=wid, classes=cls, markup=False),
                        classes="set-dyn set-slot")
        self.query_one("#set-frame", Vertical).mount(slot)
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
        self._refresh_footer()      # отчёт держит экран — футер обещает его закрытие

    def _mount_report(self, text: str) -> None:
        self._singleton("set-report", f"{text}\n\n{_REPORT_DISMISS_HINT}",
                        f"set-report set-dyn {KEEP_UNTIL_DISMISSED}")

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
                self._title_note(error)          # блок: «введите адрес» / «нужны http(s)://…»
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
        cls = "set-dyn set-flash-ok" if outcome == "ok" else "set-dyn set-flash-warn"
        self._dismiss_report()      # плашка замещает отчёт — см. `_notice`
        self._singleton("set-flash", glyph + text, f"{cls} {EPHEMERAL}")

    def _env_refs_overwritten(self, values: dict) -> tuple[list[str], list[str]]:
        """Переменные окружения, чьи ссылки в файле затрёт сохранение, разложенные
        по тому, ЧТО с ними произойдёт: (перезапись литералом, удаление пустым
        значением). Оба списка пусты — затирать нечего либо новое значение само ссылка.

        Раскладка нужна предупреждению: у литерала последствие — утечка секрета в
        файл, у пустого поля — исчезновение ссылки. Одним словом их не назвать."""
        import config_raw
        written: list[str] = []
        erased: list[str] = []
        for key, value in values.items():
            if config_raw.env_refs(value):
                continue
            refs = config_raw.env_refs(self._raw.get(key))
            (written if str(value or "").strip() else erased).extend(refs)
        return written, erased

    def _env_confirm_text(self, written: list[str], erased: list[str]) -> str:
        """Предупреждение называет ПОСЛЕДСТВИЕ, а не сам факт затирания.

        Единый текст «запишет введённое значение открытым текстом» пугал утечкой и
        на пути `Ctrl+U`, где поле пустое и ничего не пишется — ссылка удаляется.
        Оба списка разом реальны: в «Подключении» ссылками могут быть и адрес, и
        токен, и судьба у них может разойтись."""
        parts = []
        if written:
            parts.append(f"{', '.join(written)} — сохранение запишет введённое "
                         "значение в файл открытым текстом")
        if erased:
            parts.append(f"{', '.join(erased)} — сохранение удалит ссылку из файла")
        return ("В файле — ссылка на переменную окружения: " + ";  ".join(parts)
                + ".  Enter — сохранить  ·  Esc — отмена")

    def _cancel_env_confirm(self) -> None:
        """Снять confirm перезаписи ссылки. Секцию НЕ перерисовываем: ре-рендер поднял бы
        редактор заново из файла и потерял введённое значение."""
        self._pending_env_save = None
        for w_ in self.query("#set-envconfirm"):
            w_.remove()

    def _do_save(self, section: int, values: dict, *, warn_detail: str = "",
                 confirmed: bool = False, back_to_menu: bool = True) -> None:
        """Записать плоские поля секции и отчитаться (контракт §3: возврат в хаб + флэш).

        `back_to_menu=False` — остаться в секции: под-форма «резервной модели» открыта
        ИЗ списка записей, и уводить с неё в меню значит подниматься на два уровня там,
        где Esc поднимает на один. Тело секции всё равно пересобирается тем же
        `_render_view`, поэтому список сам показывает записанное."""
        import onboarding_config_writer as w
        from config import _default_config_path, validate_config_text
        from onboarding import FIELD_ANCHOR_PATHS
        if not confirmed:
            # Единственная точка записи → ссылка на переменную не может стать литералом
            # молча, каким бы путём (probe, force, секция 1) сюда ни пришли.
            written, erased = self._env_refs_overwritten(values)
            if written or erased:
                self._pending_env_save = (section, values, warn_detail)
                self._singleton("set-envconfirm",
                                Text(self._env_confirm_text(written, erased), style=WARN),
                                f"set-confirm set-dyn {KEEP_IN_VIEW}")
                return
        dotted = {FIELD_ANCHOR_PATHS.get(k, k): v for k, v in values.items()}
        try:
            w.write_values(self._config_path, dotted,
                           template_path=_default_config_path(),
                           validate=validate_config_text)
        except (ValueError, OSError) as exc:
            self._notice(f"✗ не сохранено: {exc}")
            return
        self._raw = self._read_raw()        # файл изменился — сырые значения перечитать
        # Состояние раннера снимаем ДО _on_apply: reload_config обнуляет active_runner,
        # а счётчики токенов/стоимости сессии живут в executor'е — после пересборки
        # они начинаются с нуля. Молча терять их нельзя: юзер видит цифры в футере.
        runner = getattr(self.app, "active_runner", None)
        was_running = runner is not None and runner.is_running()
        had_session = runner is not None
        try:
            self._on_apply()
            self._config = getattr(self.app, "_config", None) or self._config
        except Exception as exc:
            self._notice(f"сохранено, но конфиг не перечитан: {exc}")
            return
        # v2: возврат в хаб + флэш (контракт §3); повторный config-probe (секции 0/1)
        if back_to_menu:
            self._view = "menu"
        self._render_view()
        # Перечитать происхождение: файл изменился, оверлеи могли начать/перестать перекрывать.
        self._origins = self._read_origins()
        overridden = [k for k in values
                      if self._origin(k) is not None and self._origin(k).is_overridden]
        # Побочные последствия сохранения — общие для обеих веток: перекрытие поля не
        # отменяет ни пересборку конфига (счётчики уже обнулены), ни непройденную probe.
        extras = []
        if not was_running and had_session:
            extras.append("счётчики токенов сессии обнулены")
        if warn_detail:
            extras.append(warn_detail)
        if overridden:
            from config_origin import overlay_label
            origin = self._origin(overridden[0])
            source = overlay_label(origin)
            # «применится со следующего запуска» здесь НЕ говорим — не применится вовсе.
            # Поле называем по имени: флеш виден уже на хабе, где секции не видно.
            lines = ["✓ записано в config.yml — но сейчас не действует:",
                     f"  поле «{SECTIONS[section]}» перекрыто {source}"]
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
        text = parts[0] if len(parts) == 1 else parts[0].rstrip(".") + ". " + " ".join(parts[1:])
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
        """Подтянуть self._config из app при входе в секцию (см. _open_section):
        сама явно зовёт гейт свежести config.yml (reload_config_if_changed) —
        другое окно могло сохранить умолчание, пока этот экран уже открыт — и
        только потом берёт актуальную базу через screen_config() (тот её лишь
        отдаёт, без собственной автоперечитки).

        Совпадение по ссылке — файл не менялся с прошлой синхронизации
        (reload_config всегда создаёт новый dict, см. TuiApp.reload_config),
        сырые значения/происхождение перечитывать незачем: тот же приём хвоста
        записи, что и в _do_save/_modes_apply, только без своего
        reload — здесь его уже выполнил гейт.

        Пустая база — особый случай identity-сверки: и screen_config()
        (`self._config or {}`), и конструктор этого экрана (`config or {}`) на
        falsy-ветке аллоцируют НОВЫЙ {} при каждом обращении, так что `is` между
        двумя пустыми базами лжёт «изменилось», хотя на диске (или в тестовом
        конфиге) ничего не менялось. Ловим это явно, не ослабляя identity-ветку
        для настоящих конфигов: там reload_config даёт новый объект РОВНО когда
        файл реально изменился, и это по-прежнему единственный сигнал."""
        self.app.reload_config_if_changed()   # вход в секцию: подхватить чужую правку глобала (дизайн 2026-07-21 §4)
        fresh = self.app.screen_config()
        if fresh is self._config or (not fresh and not self._config):
            return
        self._config = fresh
        self._raw = self._read_raw()
        self._origins = self._read_origins()

    def _open_section(self, i: int) -> None:
        self._sync_config_from_app()
        self._view = i
        self._reset_draft_state()   # бампает gen — стухший воркер прежней секции отбрасывается
        self._cancel_autoprobe()    # осиротевший таймер прошлого захода в «Подключение» не копим
        # Под-состояния «Профилей» живут ровно один заход в секцию (тем же приёмом,
        # что курсор и черновики остальных): вернувшийся сюда человек не должен
        # застать чужой ввод имени, неотвеченный вопрос об удалении или чужой
        # открытый профиль. Через этот же метод идут PgUp/PgDn (`_switch_section`),
        # поэтому смена секции снимает их даром.
        self._profiles_name_prompt = None
        self._profiles_delete_name = None
        self._profiles_open = None
        if i == _UPDATE:
            self._update_idx = None     # пересчитать дефолт-курсор при (пере)входе в секцию
        self._render_view()
        # Авто-проверка при ПЕРВОМ входе в «Обновление» за сессию (как probe в др. секциях):
        # секция сразу показывает статус, а не «не проверялось». Кэш (_update_check_result)
        # переживает повторные входы → сеть не долбим; «Проверить обновления» форсит свежий.
        # На dev-установке _check_updates_now — no-op (источника обновления нет).
        if i == _UPDATE and self._update_check_result is None and not self._update_checking:
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
            self._autoprobe_timer = self.set_timer(0.05, self._autorun_connection_probe)

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
            return          # курсор шёл не с поля (галка «без авторизации») — уходить неоткуда
        if leaving_id == "conn-url" and ed.presets_open:
            return          # стрелка водила по пресет-листу, поле адреса не покидали
        url = ed.peek()[0]
        if ed.probe_snapshot() == self._last_probed_snapshot:
            return          # ровно те же значения уже проверены (или проверяются)
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
        self._autoprobe_timer = None    # таймер уже отработал
        if self._view != _CONNECTION or not self.is_mounted:
            return
        try:
            editor = self._connection_editor()
        except NoMatches:
            return                  # read-only конфиг — редактор не смонтирован, probe не нужен
        if not editor.query("#conn-url"):
            # дети редактора ещё компонуются — peek() упал бы NoMatches; повторим чуть позже
            self._autoprobe_timer = self.set_timer(0.05, self._autorun_connection_probe)
            return
        self._start_draft_probe(_CONNECTION)

    def _switch_section(self, delta: int) -> None:
        if self._view == "menu":
            return
        new = self._view + delta
        if 0 <= new < len(SECTIONS):        # края без wrap (контракт §3)
            self._menu_idx = new
            self._open_section(new)

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
            (parent if parent is not None and parent.has_class("set-slot") else w_).remove()
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
        if event.key == "escape":
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
            if self._view == _UPDATE and self._update_awaiting_confirm:
                event.stop()
                self._cancel_update()
                return
            if self._view == _UPDATE and self._reset_awaiting_confirm:
                event.stop()
                self._cancel_reset()
                return
            if self._view in _WRITE_GATED_SECTIONS and self._pending_env_save is not None:
                # Множество ШИРЕ редакторных: подтверждение перезаписи ${ENV:VAR}
                # висит и над плоской записью секции «Модели» (`agent.model`), а
                # снять его больше некому — Esc там иначе уводит в меню, оставив
                # висеть подтверждение.
                event.stop()
                self._cancel_env_confirm()     # отмена подтверждения, из секции не выходим
                return
            if self._view == _CONNECTION:
                try:
                    ed = self._connection_editor()
                except NoMatches:
                    ed = None
                if ed is not None and ed.escape_presets():
                    event.stop()                # закрыт только пресет-лист адреса, секция жива
                    self._refresh_footer()      # клавиши вернулись секции — футер за ними
                    return
            if self._view == _MODELS:
                ed = self._models_editor()
                if ed is not None and ed.escape():
                    event.stop()                # закрыто под-состояние секции, из неё не выходим
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
                self._refresh_footer()
                return
            if self._view == _PROFILES and self._profiles_name_prompt is not None:
                event.stop()                    # отмена ввода имени, из секции не выходим
                self._cancel_profile_name_prompt()
                return
            if self._view == _PROFILES and self._profiles_delete_name is not None:
                event.stop()                    # снятие подтверждения удаления — тоже своим уровнем
                self._cancel_delete_profile()
                return
            event.stop()
            self._close_or_back()
            return
        if self._view == _MCP:
            ed = self._mcp_editor()
            if ed is not None and ed.awaiting_confirm and event.key != "enter":
                event.stop()                          # confirm-баннер висит: глотаем всё, кроме
                return                                # enter (→ confirm_pending ниже) и escape (выше)
        if self._view == _UPDATE and self._update_awaiting_confirm:
            event.stop()
            if event.key == "enter":
                self._confirm_update()
            return                                     # прочие клавиши — no-op, пока висит confirm
        if self._view == _UPDATE and self._reset_awaiting_confirm:
            event.stop()
            if event.key == "enter":
                self._confirm_reset()
            return                                     # прочие клавиши — no-op, пока висит confirm
        if event.key in ("pageup", "pagedown", "ctrl+pageup", "ctrl+pagedown"):
            event.stop()                          # Ctrl+ — тихий алиас для мышечной памяти
            self._switch_section(-1 if event.key in ("pageup", "ctrl+pageup") else 1)
            return
        if event.key == "enter" and self._view in _WRITE_GATED_SECTIONS \
                and self._pending_env_save is not None:
            event.stop()                          # confirm перезаписи ссылки: Enter — подтвердил
            section, values, warn_detail = self._pending_env_save
            self._pending_env_save = None
            self._do_save(section, values, warn_detail=warn_detail, confirmed=True)
            return
        if event.key in ("tab", "shift+tab"):
            # Съедаем ВСЕГДА, даже когда переходить некуда (§4.2.6): tab/shift+tab —
            # штатные биндинги Textual (focus_next/focus_previous), и оставить их
            # фреймворку значит вернуть рассинхрон «фокус отдельно, курсор отдельно» —
            # клавиша просто молчит.
            #
            # Прежнего смысла «проверить связь» у неё здесь больше нет: проверка ушла на
            # автопробу при уходе с поля (спека §5.2) и на Ctrl+Space (§5.1).
            event.stop()
            event.prevent_default()                   # не отдавать focus-next
            return
        if event.key in ("alt+down", "ctrl+down") and self._view in _PROBE_SECTIONS:
            # Развод клавиш (спека 2026-08-06 §2): раскрытие списка — теперь ЦЕЛИКОМ
            # на Alt+↓ (и тихом синониме Ctrl+↓), Ctrl+Space ниже про список больше не
            # спрашивает. У адреса список есть (пресеты) — свёрнутый раскрываем,
            # раскрытый перечитывать нечего (статика), и клавиша просто ничего не
            # делает (не проваливаясь никуда — падать в пробу ей больше некуда).
            try:
                ed = self._connection_editor()
            except NoMatches:
                ed = None
            if ed is not None and ed.reload_presets():
                event.stop()
                self._refresh_footer()      # обещания клавиш меняются вместе со списком
            return
        if event.key in ("ctrl+at", "ctrl+space", "ctrl+@") and self._view in _PROBE_SECTIONS:
            # Развод §2: клавиша больше не спрашивает список (см. ветку Alt+↓ выше) —
            # порядок неважен, она всегда «перепроверить связь» (§2.2 спеки). Проба
            # уходит при ЛЮБЫХ полях, в том числе нетронутых: ради этого случая она и
            # заведена — «сервер лежал, поля с тех пор не трогали», где автоматическая
            # проверка по построению уже не повторится (§5.2).
            event.stop()
            if not self._writable():
                return                      # read-only: редактора нет, проверять нечего
            from onboarding import _valid_endpoint_url
            import config_raw
            url = self._connection_editor().peek()[0]
            if url and not _valid_endpoint_url(url) and not config_raw.env_refs(url):
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
                self._set_probe_line(Text("сначала выберите адрес — проверка стучится на него",
                                          style=DEEP_DIM))
                return
            self._reset_draft_state()
            self._cancel_autoprobe()                  # запускаем СВОЮ пробу — автопроба лишняя
            self._start_draft_probe(self._view)
            return
        if self._view == _MODELS:
            ed = self._models_editor()
            if ed is not None and self._route_registry_editor(ed, event):
                return
        if event.key == "enter" and self._view in _PROBE_SECTIONS:
            event.stop()
            section = self._view
            if not self._writable():
                self._notice("конфиг недоступен для записи")
                return
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
                    self._refresh_footer()       # список свернулся — футер снова про секцию
                    return                       # пресет-лист открыт — подставил/открыл, не сабмит
            values = self._collect_values(section)
            if values is None:
                return
            if not values:
                self._title_note("ничего не изменено")
                return
            if self._draft_checked is not None:       # уже проверено: решаем по вердикту
                self._smart_enter_decide(section, values, self._draft_checked)
                return
            self._cancel_autoprobe()                  # запускаем СВОЮ пробу — автопроба лишняя
            self._start_draft_probe(section, on_done=lambda r: self._smart_enter_decide(
                section, self._collect_values(section) or {}, r))
            return
        if event.key == "enter" and self._view == _UPDATE:
            event.stop()
            self._run_update_action()
            return
        if event.key in ("up", "down") and self._view == _UPDATE:
            event.stop()
            n = len(self._update_actions)
            if n > 1:
                self._update_idx = (self._update_idx + (1 if event.key == "down" else -1)) % n
                self._render_view()
            return
        if self._view == _MODES:
            from tui.toggles import group_toggle_names
            rows = group_toggle_names("rigor")
            if event.key in ("up", "down"):
                step = -1 if event.key == "up" else 1
                self._modes_idx = (self._modes_idx + step) % len(rows)
                self._render_view()
                event.prevent_default(); event.stop(); return
        if event.key in ("left", "right", "enter") and self._view == _MODES:
            event.stop()
            self._modes_apply(-1 if event.key == "left" else 1)
            return
        if self._view == _PROFILES:
            self._profiles_disk_cache = {}   # нажатие — свежий взгляд на диск
            if self._profiles_open is not None:
                # Под-вид держит клавиатуру ЦЕЛИКОМ и обрывает обработчик даже там,
                # где сам клавишу не взял: под ним лежит список профилей со своими
                # Del/r/Space, и провалившийся туда `Del` снёс бы профиль вместо
                # записи реестра. PgUp/PgDn и Esc сюда не доходят — их разбирают
                # гейты выше.
                ed = self._models_editor()
                if ed is not None and not self._route_registry_editor(ed, event):
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
                event.stop()                 # confirm-баннер висит: Enter — удалить,
                if event.key == "enter":     # прочее — no-op (канон MCP)
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
            if event.key in ("left", "right") and row["kind"] == "level":
                event.stop()
                self._level_apply(-1 if event.key == "left" else 1)
                return
            # Space — канон базы для смены состояния (MCP «вкл/выкл», отметка в
            # post-update), и активация профиля именно она. Enter остаётся тихим
            # алиасом: пока в волне A внутрь профиля входить некуда, отнимать
            # привычную клавишу не за что — но и обещать её футером нельзя, иначе
            # закрепим расклад, который сменится вместе с редактором профиля.
            if event.key in ("space", "enter"):
                event.stop()
                if row["kind"] == "level":
                    if event.key == "enter":
                        self._level_apply(1)
                elif row["kind"] == "profile":
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
                    event.stop(); return   # прочие клавиши секции — no-op, пока висит confirm (escape уже обработан выше)
                if event.key in ("up", "down"):
                    event.stop(); ed.move(-1 if event.key == "up" else 1); return
                if event.key == "space":
                    event.stop()
                    hint = ed.toggle()
                    if hint:
                        self._title_note(hint)      # у заголовка, 3с (было _notice — постоянный низ)
                    return
                if event.key == "delete" and not isinstance(self.focused, Input):
                    # Единая клавиша удаления объекта под курсором (§5.3): гейт по
                    # фокусу — полей ввода здесь сейчас нет, но правило общее для всех
                    # секций с полями.
                    event.stop(); ed.request_remove(); return
                if event.key == "ctrl+u":
                    event.stop(); ed.begin_update(); return
                if event.key == "enter":
                    event.stop(); ed.begin_enter(); return
        if event.key in ("up", "down") and self._view == _CONNECTION:
            event.stop()
            try:
                ed = self._connection_editor()
            except NoMatches:
                ed = None
            if ed is not None:
                leaving_id = getattr(self.focused, "id", None)   # с какого поля уходим
                ed.move(-1 if event.key == "up" else 1)
                # move() двигает только курсор, значений не трогает (их правки ловит
                # on_input_changed) — обесценивать им вердикт и летящую пробу нельзя.
                # Иначе вторая стрелка внутри латентности роняет пробу первой, повтор
                # не уходит (снимок совпал), и строка застревает на «проверяю…»; а
                # стрелка во время пробы Enter молча съедает сохранение, висящее у неё
                # в on_done. Ровно поэтому в мастере уход с поля поколение не бампает.
                self._cancel_env_confirm()   # но подтверждение перезаписи ссылки уход с поля снимает
                self._update_dirty()
                if self._view == _CONNECTION:
                    self._autoprobe_on_leave(ed, leaving_id)
                self._refresh_footer()   # состав клавиш зависит от того, раскрыт ли список
            return
        if event.key == "ctrl+u" and self._view == _CONNECTION:
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

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._view in _PROBE_SECTIONS:
            self._reset_draft_state()
        if self._view in (_CONNECTION, _MODELS) or self._profiles_open is not None:
            # Печать в адресе меняет фильтр пресет-листа, а с ним и обещания клавиш
            # (подставить ↔ оставить свой адрес) — футер догоняет её же событием.
            # У формы записи реестра ровно те же два списка и та же развилка — в
            # открытом профиле это та же форма, значит и то же правило.
            self._refresh_footer()
        if self._view in _EDITOR_SECTIONS:
            self._update_dirty()
