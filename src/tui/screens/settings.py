"""Хаб настроек /settings (спека B, модель A+; визуальный контракт §2-3).

Меню секций → Enter открывает тело секции (свап идиомой .set-dyn), Esc — назад
в меню (из меню — закрыть экран), ←/→ — соседняя секция БЕЗ выхода в меню
(только когда фокус не в текстовом поле). Push-экран поверх TraceScreen."""
from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import Input, Rule, Static
from rich.text import Text

from tui.palette import ACCENT, DEEP_DIM, DIM, ERR, INK, OK, WARN
from tui.widgets.loading_wave import WAVE_INTERVAL, WAVE_SEQ, wave_glyph

SECTIONS = ("Подключение", "Модель", "Профили", "MCP-серверы", "Режимы работы", "Обновление")

_CONNECTION, _MODEL, _PROFILES, _MCP, _MODES, _UPDATE = 0, 1, 2, 3, 4, 5
_EDITOR_SECTIONS = {_CONNECTION, _MODEL}   # редакторные: writable, dirty-tracked, submit
_PROBE_SECTIONS = {_CONNECTION}            # server-probe: ручная проверка, глиф из config-probe

_FOOTERS = {
    "menu": "↑/↓ — выбор  ·  Enter — открыть  ·  Esc — закрыть",
    # Пресет-лист адреса открывается сам почти всегда (пусто/совпало с пресетом,
    # см. комментарий у `_FOOTER_PRESET_LIST` ниже) — но когда он свёрнут (свой
    # адрес, не встретившийся в пресетах), футер обязан назвать способ вернуть его
    # (спека 2026-08-06 §4.4): без этого `Esc` уводил бы список в один конец без
    # обратного пути. Дописывание `Alt+↓ — показать список` вырастило строку со 108 до
    # 136 символов — на самом узком окне (`_FOOTER_MIN_COLS`, сторож
    # `test_all_footers_fit_the_two_line_budget`) она рвалась на ТРИ строки, и хвост
    # «Esc — меню» срезался высотой `#set-footer` (2). Жертва по правилу §4.2 спеки:
    # «↑/↓ — навигация» снята ПЕРВОЙ — курсор и так виден подсветкой поля, а не третьей
    # (не называть клавишу) — до неё дело не дошло, снятого сегмента хватило.
    _CONNECTION: ("Enter — сохранить  ·  Alt+↓ — показать список  ·  "
                  "Ctrl+Space — проверить связь  ·  PgUp/PgDn — секции  ·  Esc — меню"),
    # Alt+↓ назван (не Ctrl+Space — та роль ушла клавише проверки связи, спека
    # 2026-08-06 §2): он перечитывает список моделей и здесь (`ModelEditor.reload`),
    # а своей строки-подсказки у этого списка нет (`hint=None` — под рамкой рисуется
    # блок статуса). Молчание о работающей клавише — та же ложь, что обещание
    # неработающей (§6): при лежавшем сервере секция выглядела бы безнадёжно сломанной.
    # Ctrl+Space в этой секции пробы не запускает и в футере не обещается (§5.2 спеки).
    _MODEL: "↑/↓ — выбор  ·  Enter — сохранить  ·  Alt+↓ — перечитать список  ·  PgUp/PgDn — секции  ·  Esc — меню",
    # «Обновить версию», а не просто «обновить»: Ctrl+U ставит пакет заново (меняет
    # систему), а Alt+↓ только перечитывает — одним словом на двоих они неразличимы (§5.4).
    _MCP: "↑/↓ · Enter — установить · Space — вкл/выкл · Ctrl+U — обновить версию · Del — удалить · PgUp/PgDn — секции · Esc",
    _MODES: "↑/↓ — выбор · ←/→/Enter — переключить · PgUp/PgDn — секции · Esc — меню",
    _UPDATE: "↑/↓ · Enter — выполнить · PgUp/PgDn — секции · Esc — меню",
    _PROFILES: "↑/↓ — выбор  ·  Enter — активировать  ·  PgUp/PgDn — секции  ·  Esc — меню",
}
_FOOTER_READONLY = "PgUp/PgDn — секции  ·  Esc — меню"
# Раскрытый пресет-лист адреса забирает клавиши себе — футер на это время становится
# его (§6.4, канон мастера `onboarding._FOOTER_PRESET_LIST`). Состояние не редкое, а
# ОБЫЧНОЕ: список раскрывается сам на входе в секцию, как только адрес пуст (чистая
# установка) или совпал с пресетом (все, кто выбирал его из списка).
# «↑/↓ — выбор», а не «выбор провайдера» (как зовёт то же место мастер): у `#set-footer`
# ширина минус padding, и полная формулировка давала ровно 120 — на канонных 120
# колонках хвост «секции» срывался на вторую строку в самом частом состоянии секции.
# Слово то же, что у списка моделей рядом (`_FOOTERS[_MODEL]`), а что именно выбирают,
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
_FOOTER_PROFILES_LEVEL = ("↑/↓ — выбор  ·  ←/→/Enter — переключить  ·  "
                          "PgUp/PgDn — секции  ·  Esc — меню")
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


class SettingsScreen(Screen):
    def __init__(self, *, config, config_path, on_apply, probe_fn=None) -> None:
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
        from tui.services.settings_probe import probe as _default_probe
        self._probe_fn = probe_fn or _default_probe
        self._probe_config = None       # ProbeResult сохранённых значений → глифы хаба
        self._config_gen = 0            # generation config-probe: устаревшие результаты отбрасываются
        self._draft_gen = 0             # generation draft-probe: независим от config-probe
        self._autoprobe_timer = None    # таймер автопробы секции «Подключение» (см. _cancel_autoprobe)
        self._last_probed_snapshot = None   # (url, token, proxy) последней пробы — дребезг автопробы
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
        if self._view == "menu":
            return _FOOTERS["menu"]
        if self._view in _EDITOR_SECTIONS and not self._writable():
            return _FOOTER_READONLY
        if self._view == _CONNECTION and self._presets_open():
            return (_FOOTER_PRESET_LIST if self._presets_have_selection()
                    else _FOOTER_PRESET_LIST_NO_MATCH)
        if self._view == _CONNECTION:
            # Список свёрнут: Alt+↓ обещаем, только пока курсор на адресе — на «Токене»/
            # «Прокси» клавиша молчит (`on_url_field` гейтит ровно эту позицию,
            # находка финального ревью 2026-08-06, D), и обещать её там значило бы врать.
            return _FOOTERS[_CONNECTION] if self._connection_on_url() else _FOOTER_CONNECTION_FIELD
        if self._view == _PROFILES:
            return _FOOTER_PROFILES_LEVEL if self._profiles_on_level_row() else _FOOTERS[_PROFILES]
        return _FOOTERS.get(self._view, _FOOTER_READONLY)

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
        elif i == _MODEL:
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
        """Text глифа строки: анимированная волна в полёте probe (секции 0-2),
        иначе одиночный глиф ✓/!/— из _menu_glyph."""
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

    def _menu_glyph(self, i: int) -> tuple[str, str]:
        """(глиф, style) для строки меню. … в полёте; ✓/!/— по §2 контракта.
        Глиф — вердикт probe о ДЕЙСТВУЮЩИХ (резолвнутых) значениях: он про то, работает
        ли конфиг, а не про то, что записано в файле (это показывает столбец значения)."""
        if i in _EDITOR_SECTIONS:
            if self._probe_in_flight:
                return "…", DEEP_DIM
            r = self._probe_config
            c = self._config
            if i == _CONNECTION:
                from onboarding import TOKEN_SENTINEL
                endpoint, _a, _v, _m, _p = self._probe_args_from_config()
                if not endpoint:
                    return "—", DEEP_DIM
                if r is None:
                    return "—", DEEP_DIM
                reachable = r.verdict == "ok" or (r.verdict == "rejected" and r.http_status == 401)
                if not reachable:
                    return "!", ERR                  # сервер недоступен СЕЙЧАС
                auth = self._raw.get("Authorization")
                if not auth:
                    return "✓", OK                   # без авторизации — решает только достижимость
                if auth == TOKEN_SENTINEL:
                    return "—", DEEP_DIM             # токен не задан (шаблон) — нечем подтвердить
                return ("✓", OK) if r.auth_ok else ("!", ERR)   # токен принят/отклонён (401)
            model = c.get("model") or ""
            if not model or r is None:
                return "—", DEEP_DIM
            if r.models is None:
                return "—", DEEP_DIM
            ids = [m.id for m in r.models]
            return ("✓", OK) if model in ids else ("—", DEEP_DIM)   # не в списке → —, не !
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
            title.update("Настройки")
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
            elif self._view == _PROFILES:
                self.call_after_refresh(self._refresh_profiles_hint)
                self.call_after_refresh(self._refresh_profiles_rows)
        # Футер — про тело, поэтому считается ПОСЛЕ него (канон мастера
        # `_mount_step_body`): состав тела задаёт и состав обещаний.
        self._refresh_footer()
        if self._view == _CONNECTION:
            # И ещё раз, когда дерево устоялось: `ConnectionEditor` раскрывает пресет-лист
            # в СВОЁМ on_mount — это уже после вызова выше, и первый кадр секции обещал бы
            # «Enter — сохранить» поверх раскрытого списка. Только этой секции: отложенный
            # пересчёт «вслепую» опасен там, где содержимое приезжает из сети (список
            # моделей) — он лёг бы между раскрытием списка и приездом строк и оставил бы
            # футер на «совпадений нет» до следующего события.
            self.call_after_refresh(self._refresh_footer)

    # --- dirty-маркер заголовка (спека v2 §3.1, Task 7) ---
    def _update_dirty(self) -> None:
        """Обновляет #set-title: имя секции (+ dirty-маркер « изменено» для редакторов 0–2)."""
        if self._view == "menu":
            return
        try:
            title = self.query_one("#set-title", Static)
        except NoMatches:
            return
        name = SECTIONS[self._view]
        if self._view in _EDITOR_SECTIONS and self._is_dirty(self._view):
            t = Text(name)
            t.append("  изменено", style=WARN)
            title.update(t)
        else:
            title.update(name)

    def _title_note(self, text: str) -> None:
        """Кратковременная спокойная реплика в слоте заголовка (там же, где «изменено»).
        Гаснет через _TITLE_NOTE_SEC; повторный вызов перезапускает таймер."""
        if self._view == "menu":
            return
        try:
            title = self.query_one("#set-title", Static)
        except NoMatches:
            return
        t = Text(SECTIONS[self._view])
        t.append("  " + text, style=DEEP_DIM)
        title.update(t)
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
        from tui.editors.model import ModelEditor
        try:
            picked = self.query_one(ModelEditor).peek()
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
            from tui.services.settings_probe import ProbeResult
            try:
                result = self._probe_fn(
                    endpoint, auth, verify, model, config_proxy=proxy,
                )
            except Exception as exc:
                result = ProbeResult("unknown", None, None, 0, f"внутренняя ошибка проверки: {exc}")
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
            if r.verdict == "unknown" and not r.http_status:
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
        else:
            token = auth_cfg                         # поле пусто → действующий токен
        has_token = bool(token)
        # draft proxy: поле → иначе config; пустое поле = «не задано» → env через резолвер
        proxy = proxy_draft if proxy_draft else proxy_cfg
        # Снимок ПОЛЕЙ (не резолва) — по нему автопроба ухода с поля отличает
        # «значения изменились» от ходьбы по форме; пишем здесь, а не на местах
        # вызова, чтобы автопроба входа в секцию и ручные пробы тоже считались.
        self._last_probed_snapshot = (url, token_raw, proxy_draft)
        self._draft_gen += 1
        gen = self._draft_gen
        self._set_probe_line(Text("проверяю…", style=DEEP_DIM))

        def _work() -> None:
            from tui.services.settings_probe import ProbeResult, interpret_auth
            try:
                control = self._probe_fn(
                    endpoint, None, verify, model, config_proxy=proxy,
                )
                with_token = None
                if control.verdict == "rejected" and control.http_status == 401 and has_token:
                    with_token = self._probe_fn(
                        endpoint, token, verify, model, config_proxy=proxy,
                    )
                result = interpret_auth(control, with_token, has_token)
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
        невалидность, её ловит _collect_values→submit). Вердикт сервера — информация."""
        if not values:
            self._title_note("ничего не изменено")
            return
        if r is not None and r.verdict == "rejected":
            self._do_save(section, values,
                          warn_detail="сервер отклонил токен — проверьте")
        elif r is not None and r.verdict == "unknown":
            self._do_save(section, values,
                          warn_detail="проверить не удалось — сервер недоступен")
        else:
            self._do_save(section, values)

    # --- guard записи ---
    def _writable(self) -> bool:
        from pathlib import Path
        from config import _default_config_path
        p = self._config_path
        if not p:
            return False
        try:
            return Path(p).resolve() != Path(_default_config_path()).resolve()
        except OSError:
            return False

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
        запускали) писал бы `profile:`/`level:` внутрь установленного пакета."""
        from pathlib import Path
        from config import _default_config_path
        try:
            return Path(target).resolve() != Path(_default_config_path()).resolve()
        except OSError:
            return False

    _TEMPLATE_WRITE_REFUSAL = (
        "✗ не сохранено: конфиг недоступен для записи — используется пакетный "
        "шаблон (настройте GC_HOME или запустите gc-ua --init-config)"
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

    def _mount_field_hints(self, frame: Vertical, key: str) -> None:
        """Подписи под полем — только когда есть что сказать.

        Ссылка ${ENV:VAR} → её статус. Поле перекрыто оверлеем → действующее значение
        и источник. Случаи складываются: в файле ссылка И поле перекрыто → обе строки.
        Обычное значение из базового файла → подписи нет, экран чист."""
        from onboarding import display_value, env_ref_status
        raw = self._raw_value(key)
        if env_ref_status(raw):
            frame.mount(Static(display_value(raw), classes="set-hint set-dyn"))

        origin = self._origin(key)
        if origin is not None and origin.is_overridden:
            from config_origin import overlay_label
            source = overlay_label(origin)
            shown = self._effective_display(key, origin.effective)
            frame.mount(Static(
                f"⚠ действует {shown} — перекрыто {source}",
                classes="set-hint set-dyn set-warnflash"))

    def _mount_section_body(self, frame: Vertical, section: int) -> None:
        from onboarding import TOKEN_SENTINEL
        if section in _EDITOR_SECTIONS and not self._writable():
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
        elif section == _MODEL:
            from tui.editors.model import ModelEditor
            from model_picker import fetch_models
            from tui.services.model_list_cache import make_cached_fetch
            # За списком моделей ходим ДЕЙСТВУЮЩИМИ (резолвнутыми) адресом и токеном;
            # current/active — сырые: именно они пишутся в файл.
            endpoint, auth, verify, _m, proxy = self._probe_args_from_config()
            model = self._raw_value("model")
            origin = self._origin("model")
            frame.mount(ModelEditor(
                current=model, endpoint=endpoint, auth=auth, verify=verify,
                fetch=make_cached_fetch(fetch_models), active=model, on_loaded=self._update_dirty,
                config_proxy=proxy,
                # Перекрытую модель в списке нельзя звать «текущей»: действует оверлей,
                # а подпись ниже говорит именно это — экран не должен спорить сам с собой.
                active_overridden=bool(origin is not None and origin.is_overridden)))
            self._mount_field_hints(frame, "model")
            self._mount_shadow_hint(frame)
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

    def _shadow_hint_text(self) -> str | None:
        """«действует профиль work / реестр моделей — эти поля база для незаданного»
        в секциях «Подключение»/«Модель» — плоские ключи легаси остаются источником
        наследования, но не действующим резолвом, когда есть merged-реестр (с
        профилем или без него) или активный профиль.

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
            # set-spaced — воздух между строками, тот же класс/паттерн, что и в
            # секции «Обновление» (margin-bottom: 1 на каждой строке).
            cls = "set-row set-dyn set-spaced" + (" selected" if selected else "")
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
        rows.append({"kind": "level"})
        return rows

    def _profiles_on_level_row(self) -> bool:
        """Курсор секции «Профили» — на строке уровня (последняя, всегда есть):
        футер тогда обещает ←/→/Enter вместо Enter-активации (позиционный
        футер, тот же приём, что у `_CONNECTION`)."""
        rows = self._profiles_rows()
        return bool(rows) and self._profiles_idx == len(rows) - 1

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
        """Enter на «— без профиля»: деактивация записью `profile: ""` по единому
        правилу записи; «ничего не активно» — честный no-op."""
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
        """Enter на строке профиля. «Уже активен» — сравнение с КОНФИГ-указателем
        (`self._config["profile"]`, то, что реально пишет Enter), не с сессионным
        выбором: сессия — отдельный механизм (`/profiles`), запись файла её не трогает."""
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
            return f"Enter — деактивировать (запись: {self._deactivation_target()})"
        name, ref = row["name"], row["ref"]
        lines = [str(ref.path)]
        target = self._profile_activation_target(ref)
        lines.append(f"Enter — активировать (запись: {target})")
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
        rows = self._profiles_rows()
        self._profiles_idx = min(self._profiles_idx, len(rows) - 1)
        profile_rows = [r for r in rows if r["kind"] == "profile"]
        if not profile_rows:
            frame.mount(Static(
                "Профиль — отдельный файл-конфиг: переключаемый набор ЛЮБЫХ "
                "настроек — модели, уровни, гварды и любые другие секции конфига",
                classes="set-hint set-dyn"))
            frame.mount(Static(
                "Чтобы создать профиль — сохраните файл ~/.gc/profiles/<имя>.yml, "
                "например:",
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
        else:
            for i, row in enumerate(rows):
                if row["kind"] == "level":
                    continue
                selected = i == self._profiles_idx
                cls = "set-row set-dyn set-spaced set-profiles-row" + (" selected" if selected else "")
                text = (self._none_row_text(rows, selected) if row["kind"] == "none"
                        else self._profile_row_text(row, rows, selected))
                frame.mount(Static(text, classes=cls))
            # Черта перед строкой уровня: уровень — не «ещё один профиль»,
            # отделяем как группы «Режимов работы».
            frame.mount(Rule(classes="set-group-sep"))
        level_idx = len(rows) - 1
        selected = level_idx == self._profiles_idx
        # в пустом состоянии сверху скелет-подсказка без нижнего отступа —
        # даём строке уровня воздух сверху и снизу (set-spaced-gap, не пара
        # раздельных классов: longhand-margins в Textual перетирают друг друга)
        spacing = "set-spaced" if profile_rows else "set-spaced-gap"
        cls = f"set-row set-dyn {spacing} set-profiles-row" + (" selected" if selected else "")
        frame.mount(Static(self._level_row_text(selected), classes=cls))
        frame.mount(Static(self._profiles_hint_text(rows),
                           classes="set-hint set-dyn set-profiles-hint"))

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
            else:
                fresh[i].update(self._profile_row_text(row, rows, selected))

    def _refresh_profiles_hint(self) -> None:
        if self._view != _PROFILES:
            return
        rows = self._profiles_rows()
        widgets = list(self.query(".set-profiles-hint"))
        if widgets:
            widgets[-1].update(self._profiles_hint_text(rows))

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
                               classes="set-confirm set-dyn"))
        elif self._reset_awaiting_confirm:
            frame.mount(Static(Text(self._reset_confirm_text(), style=WARN),
                               classes="set-confirm set-dyn"))
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
        self._notice("\n".join(lines))

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
        self._notice("\n".join(lines))

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
        при следующем `_render_view`."""
        for w in self.query(f"#{wid}"):
            if w._pruning:
                continue
            w.update(text)
            w.set_classes(cls)
            return
        self.query_one("#set-frame", Vertical).mount(
            Vertical(Static(text, id=wid, classes=cls), classes="set-dyn set-slot"))

    def _notice(self, text: str) -> None:
        self._singleton("set-notice", text, "set-hint set-dyn")

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
        elif section == _MODEL:
            from tui.editors.model import ModelEditor
            value = self.query_one(ModelEditor).submit()
            if value is not None:
                values["model"] = value
        return values

    def _flash(self, text: str, *, outcome: str = "ok") -> None:
        """Заметная плашка результата (по языку confirm-баннера): фон + левый бордер +
        ведущий глиф, окраска по исходу. outcome: "ok" (зелёная ✓) | "warn" (янтарная ⚠)."""
        glyph = "✓ " if outcome == "ok" else "⚠ "
        cls = "set-dyn set-flash-ok" if outcome == "ok" else "set-dyn set-flash-warn"
        self._singleton("set-flash", glyph + text, cls)

    def _env_refs_overwritten(self, values: dict) -> list[str]:
        """Переменные окружения, чьи ссылки в файле затрёт вводимый литерал
        (пусто — затирать нечего либо новое значение само ссылка)."""
        import config_raw
        out: list[str] = []
        for key, value in values.items():
            if config_raw.env_refs(value):
                continue
            out.extend(config_raw.env_refs(self._raw.get(key)))
        return out

    def _env_confirm_text(self, refs: list[str]) -> str:
        return (f"В файле — ссылка на переменную окружения ({', '.join(refs)}); "
                "сохранение запишет введённое значение в файл открытым текстом.  "
                "Enter — сохранить  ·  Esc — отмена")

    def _cancel_env_confirm(self) -> None:
        """Снять confirm перезаписи ссылки. Секцию НЕ перерисовываем: ре-рендер поднял бы
        редактор заново из файла и потерял введённое значение."""
        self._pending_env_save = None
        for w_ in self.query("#set-envconfirm"):
            w_.remove()

    def _do_save(self, section: int, values: dict, *, warn_detail: str = "",
                 confirmed: bool = False) -> None:
        import onboarding_config_writer as w
        from config import _default_config_path, validate_config_text
        from onboarding import FIELD_ANCHOR_PATHS
        if not confirmed:
            # Единственная точка записи → ссылка на переменную не может стать литералом
            # молча, каким бы путём (probe, force, секция 1) сюда ни пришли.
            refs = self._env_refs_overwritten(values)
            if refs:
                self._pending_env_save = (section, values, warn_detail)
                self._singleton("set-envconfirm", Text(self._env_confirm_text(refs), style=WARN),
                                "set-confirm set-dyn")
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
        url, _auth_idx, token_raw, proxy = ed.peek()
        if (url, token_raw, proxy) == self._last_probed_snapshot:
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

    def on_key(self, event: events.Key) -> None:
        for w_ in self.query("#set-flash"):
            w_.remove()
        if event.key == "escape":
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
            if self._view in _EDITOR_SECTIONS and self._pending_env_save is not None:
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
        if event.key == "enter" and self._view in _EDITOR_SECTIONS and self._pending_env_save is not None:
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
        if event.key in ("alt+down", "ctrl+down") and self._view == _MODEL:
            # Карта проб §2.5: под курсором поле со списком С СЕРВЕРА — клавиша идёт
            # за списком заново. Раскрытием тут не обойтись: свёртывания у этого
            # списка нет вовсе (§9.1), рамка стоит всегда — клавиша съедается
            # безусловно.
            event.stop()
            from tui.editors.model import ModelEditor
            try:
                self.query_one(ModelEditor).reload()
            except NoMatches:
                pass                    # секция ещё собирается — перечитывать нечего
            return
        if event.key in ("ctrl+at", "ctrl+space", "ctrl+@") and self._view == _MODEL:
            # Пробы в «Модели» нет (§2.5, §5.2 спеки 2026-08-06): раскрытие/перечитывание
            # списка ушло на Alt+↓ выше, а связь проверяют в «Подключении». Клавиша
            # съедается молча — не проваливается в глобальные биндинги и ничего не
            # обещает.
            event.stop()
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
                # автопробу здесь НЕ трогаем: список открыт по умолчанию (пустой адрес
                # или совпадение с пресетом — обычное дело на чистой установке), и если
                # Enter поглощён им, своя проба не стартует — без автопробы вердикт связи
                # не появился бы вовсе, до первого Ctrl+Space или правки с Enter.
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
        if event.key == "enter" and self._view == _MODEL:
            event.stop()
            section = self._view
            if not self._writable():
                self._notice("конфиг недоступен для записи")
                return
            values = self._collect_values(section)
            if values is None:
                return
            if not values:
                self._title_note("ничего не изменено")
                return
            self._do_save(section, values)
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
            if event.key == "enter":
                event.stop()
                if row["kind"] == "level":
                    self._level_apply(1)
                elif row["kind"] == "profile":
                    self._activate_profile(row["name"], row["ref"])
                elif row["kind"] == "none":
                    self._deactivate_profile()
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
        if event.key in ("up", "down") and self._view in {_CONNECTION, _MODEL}:
            event.stop()
            ed = None
            if self._view == _CONNECTION:
                try:
                    ed = self._connection_editor()
                except NoMatches:
                    pass
            elif self._view == _MODEL:
                try:
                    from tui.editors.model import ModelEditor
                    ed = self.query_one(ModelEditor)
                    if not (ed.models_loaded and ed.has_models):
                        ed = None
                except NoMatches:
                    pass
            if ed is not None:
                leaving_id = getattr(self.focused, "id", None)   # с какого поля уходим
                ed.move(-1 if event.key == "up" else 1)
                if self._view == _MODEL:
                    # move() здесь МЕНЯЕТ выбранную модель — вердикт и летящая проба
                    # были про другое значение.
                    self._reset_draft_state()
                else:
                    # «Подключение»: move() двигает только курсор, значений не трогает
                    # (их правки ловит on_input_changed) — обесценивать им вердикт и
                    # летящую пробу нельзя. Иначе вторая стрелка внутри латентности
                    # роняет пробу первой, повтор не уходит (снимок совпал), и строка
                    # застревает на «проверяю…»; а стрелка во время пробы Enter молча
                    # съедает сохранение, висящее у неё в on_done. Ровно поэтому в
                    # мастере уход с поля поколение не бампает.
                    self._cancel_env_confirm()   # но подтверждение перезаписи ссылки уход с поля снимает
                self._update_dirty()
                if self._view == _CONNECTION:
                    self._autoprobe_on_leave(ed, leaving_id)
                self._refresh_footer()   # вход на адрес раскрывает список, уход — сворачивает
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
        if self._view == _CONNECTION:
            # Печать в адресе меняет фильтр пресет-листа, а с ним и обещания клавиш
            # (подставить ↔ оставить свой адрес) — футер догоняет её же событием.
            self._refresh_footer()
        if self._view in _EDITOR_SECTIONS:
            self._update_dirty()
