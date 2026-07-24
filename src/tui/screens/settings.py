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
from textual.widgets import Input, Static
from rich.text import Text

from tui.palette import ACCENT, DEEP_DIM, DIM, ERR, INK, OK, WARN
from tui.widgets.loading_wave import WAVE_INTERVAL, WAVE_SEQ, wave_glyph

SECTIONS = ("Подключение", "Модель", "MCP-серверы", "Режимы работы", "Обновление")

_CONNECTION, _MODEL, _MCP, _MODES, _UPDATE = 0, 1, 2, 3, 4
_EDITOR_SECTIONS = {_CONNECTION, _MODEL}   # редакторные: writable, dirty-tracked, submit
_PROBE_SECTIONS = {_CONNECTION}            # server-probe: Tab-проверка, глиф из config-probe

_FOOTERS = {
    "menu": "↑/↓ — выбор  ·  Enter — открыть  ·  Esc — закрыть",
    0: "↑/↓ — навигация  ·  Enter — сохранить  ·  Tab — проверить соединение  ·  PgUp/PgDn — секции  ·  Esc — меню",
    1: "↑/↓ — выбор  ·  Enter — сохранить  ·  PgUp/PgDn — секции  ·  Esc — меню",
    2: "↑/↓ · Enter — установить · Space — вкл/выкл · Ctrl+U — обновить · Ctrl+D — удалить · PgUp/PgDn — секции · Esc",
    _MODES: "↑/↓ — выбор · ←/→/Enter — переключить · PgUp/PgDn — секции · Esc — меню",
    4: "↑/↓ · Enter — выполнить · PgUp/PgDn — секции · Esc — меню",
}
_FOOTER_READONLY = "PgUp/PgDn — секции  ·  Esc — меню"

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


class SettingsScreen(Screen):
    def __init__(self, *, config, config_path, on_apply, probe_fn=None) -> None:
        super().__init__()
        self._config = config or {}
        self._config_path = config_path
        self._raw = self._read_raw()    # значения полей ИЗ ФАЙЛА, без резолва (см. _read_raw)
        self._origins = self._read_origins()   # происхождение полей: чем перекрыто, что действует
        self._on_apply = on_apply
        self._pending_env_save = None   # (section, values, warn_detail) — ждёт confirm перезаписи ссылки
        self._view: object = "menu"     # "menu" | int(0..4)
        self._menu_idx = 0
        self._modes_idx = 0             # курсор строки подэкрана «Режимы работы»
        from tui.services.settings_probe import probe as _default_probe
        self._probe_fn = probe_fn or _default_probe
        self._probe_config = None       # ProbeResult сохранённых значений → глифы хаба
        self._config_gen = 0            # generation config-probe: устаревшие результаты отбрасываются
        self._draft_gen = 0             # generation draft-probe: независим от config-probe
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
        import app_version
        return f"gc-ua · v{app_version.agent_version()}"

    def _title_text(self) -> str:
        if self._view == "menu":
            return "Настройки"
        return f"Настройки · {SECTIONS[self._view]}"

    def _footer_text(self) -> str:
        if self._view == "menu":
            return _FOOTERS["menu"]
        if self._view in _EDITOR_SECTIONS and not self._writable():
            return _FOOTER_READONLY
        return _FOOTERS.get(self._view, _FOOTER_READONLY)

    def on_mount(self) -> None:
        # Вердикт стартового авто-чека (app) — общий на сессию: хаб показывает статус сразу,
        # а секция не ходит в сеть второй раз (Enter на «Проверить обновления» — форсит).
        self._update_check_result = getattr(self.app, "_update_check_result", None)
        endpoint, _a, _v, _m = self._probe_args_from_config()
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
            from tui.toggles import TOGGLES, toggle_state
            pairs = [f"{spec.label}: {toggle_state(self.app, name)}" for name, spec in TOGGLES.items()]
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
                endpoint, _a, _v, _m = self._probe_args_from_config()
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
        return "—", DEEP_DIM

    # --- рендер (полная замена тела фрейма, кроме персистентного #set-title) ---
    def _render_view(self) -> None:
        self.query_one("#header-left", Static).update(self._title_text())
        self.query_one("#set-footer", Static).update(self._footer_text())
        frame = self.query_one("#set-frame", Vertical)
        title = self.query_one("#set-title", Static)
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
            from tui.editors.connection import ConnectionEditor
            try:
                return self.query_one(ConnectionEditor).is_dirty()
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
        from onboarding import TOKEN_SENTINEL, URL_SENTINEL
        c = self._config
        url = c.get("completions_url") or ""
        endpoint = url if url and url != URL_SENTINEL else ""
        auth = (c.get("chat_completion_headers") or {}).get("Authorization")
        if auth in (TOKEN_SENTINEL, ""):
            auth = None
        return endpoint, auth, bool(c.get("use_ssl", True)), c.get("model") or ""

    def _start_config_probe(self) -> None:
        endpoint, auth, verify, model = self._probe_args_from_config()
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
                result = self._probe_fn(endpoint, auth, verify, model)
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

    # --- draft-probe секции (Tab / умный Enter, спека v2 §3.1) ---
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
        from tui.editors.connection import ConnectionEditor
        from onboarding import authorization_header
        endpoint_cfg, auth_cfg, verify, model = self._probe_args_from_config()
        url, auth_idx, token_raw, _proxy = self.query_one(ConnectionEditor).peek()
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
        self._draft_gen += 1
        gen = self._draft_gen
        self._set_probe_line(Text("проверяю…", style=DEEP_DIM))

        def _work() -> None:
            from tui.services.settings_probe import ProbeResult, interpret_auth
            try:
                control = self._probe_fn(endpoint, None, verify, model)
                with_token = None
                if control.verdict == "rejected" and control.http_status == 401 and has_token:
                    with_token = self._probe_fn(endpoint, token, verify, model)
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
            from config_origin import OVERLAY_LABELS
            source = OVERLAY_LABELS[origin.overlay_kind]
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
            frame.mount(ConnectionEditor(
                url_current=self._raw_value("completions_url"),
                auth_header=header,
                proxy_current=self._raw_value("proxy"),
                framed=True))
            self._mount_field_hints(frame, "completions_url")
            self._mount_field_hints(frame, "Authorization")
            self._mount_field_hints(frame, "proxy")
            frame.mount(Static(self._probe_line_text(self._probe_config, _CONNECTION),
                               classes="set-hint set-dyn set-probe"))
        elif section == _MODEL:
            from tui.editors.model import ModelEditor
            from model_picker import fetch_models
            # За списком моделей ходим ДЕЙСТВУЮЩИМИ (резолвнутыми) адресом и токеном;
            # current/active — сырые: именно они пишутся в файл.
            endpoint, auth, verify, _m = self._probe_args_from_config()
            model = self._raw_value("model")
            origin = self._origin("model")
            frame.mount(ModelEditor(
                current=model, endpoint=endpoint, auth=auth, verify=verify,
                fetch=fetch_models, active=model, on_loaded=self._update_dirty, framed=True,
                # Перекрытую модель в списке нельзя звать «текущей»: действует оверлей,
                # а подпись ниже говорит именно это — экран не должен спорить сам с собой.
                active_overridden=bool(origin is not None and origin.is_overridden)))
            self._mount_field_hints(frame, "model")
        elif section == _MCP:
            from tui.editors.mcp_section import McpSectionEditor
            from tui.services.mcp_manager import McpManager
            mgr = McpManager(user_config=self._config)
            frame.mount(McpSectionEditor(manager=mgr, on_change=self._mcp_changed))
        elif section == _MODES:
            self._mount_modes_section(frame)
        else:
            self._mount_update_section(frame)

    # --- секция «Режимы работы» (дизайн 2026-07-18-settings-work-modes-design §3-4) ---
    def _modes_columns(self) -> tuple[int, int]:
        """(label_w, track_w) — общие колонки строк подэкрана: `label_w` — ширина
        самого длинного `ToggleSpec.label` + 3 (как имя-колонка палитры, канон §4)
        + 2 на курсор `▸ `/`  ` (конвенция меню/«Обновления» — курсор перед label,
        колонки трека остаются общими для всех строк); `track_w` — ширина самого
        широкого трека (`render_track`) среди TOGGLES — короткие треки внутри поля
        прижимаются вправо (канон §4)."""
        from tui.toggles import TOGGLES, render_track
        specs = list(TOGGLES.values())
        label_w = max(len(spec.label) for spec in specs) + 3 + 2
        track_w = max(len(render_track(spec, spec.names[0]).plain) for spec in specs)
        return label_w, track_w

    def _mode_row_text(self, name: str, selected: bool) -> Text:
        from tui.toggles import TOGGLES, render_track, toggle_state
        spec = TOGGLES[name]
        current = toggle_state(self.app, name)
        label_w, track_w = self._modes_columns()
        track = render_track(spec, current)
        t = Text()
        t.append("▸ " if selected else "  ", style=ACCENT)   # курсор — конвенция меню/«Обновления»
        t.append(spec.label.ljust(label_w - 2), style=INK)
        t.append(" " * (track_w - len(track.plain)))   # трек прижат вправо в общем поле
        t.append_text(track)
        t.append("  ")
        t.append(current, style=INK)
        return t

    def _mount_modes_section(self, frame: Vertical) -> None:
        from tui.toggles import TOGGLES
        names = list(TOGGLES.keys())
        if self._modes_idx >= len(names):
            self._modes_idx = 0
        for i, name in enumerate(names):
            selected = i == self._modes_idx
            # set-spaced — воздух между строками, тот же класс/паттерн, что и в
            # секции «Обновление» (margin-bottom: 1 на каждой строке).
            cls = "set-row set-dyn set-spaced" + (" selected" if selected else "")
            frame.mount(Static(self._mode_row_text(name, selected), classes=cls))
        # Подсказка выделенного тоггла — ВНУТРИ рамки (конвенция «Обновления»:
        # пояснительная строка в карточке под строками-опциями, класс set-hint).
        # Без id: frame.remove() старых детей отложенный (не синхронный) — id тут
        # же коллизировал бы с ещё не отсоединённым предыдущим Static (DuplicateIds).
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
        """Подсказка выделенного тоггла внутри рамки (канон §6, без клавишной части —
        клавиши показывает статичный футер вне рамки): значение + подсказка
        ПРИМЕНЁННОГО состояния, приоритетно усечённые `_fit_value_hint`."""
        from tui.toggles import _fit_value_hint, TOGGLES, toggle_state
        names = list(TOGGLES.keys())
        if not names:
            return ""
        name = names[self._modes_idx] if self._modes_idx < len(names) else names[0]
        spec = TOGGLES[name]
        current = toggle_state(self.app, name)
        idx = spec.names.index(current) if current in spec.names else 0
        val = spec.values[idx]
        return _fit_value_hint(val.value, val.hint, self._modes_hint_width())

    def _refresh_modes_hint(self) -> None:
        """Carry-fix (тот же приём, что и `_refresh_menu_rows`): досчитать усечение
        hint-строки ПОСЛЕ layout-прохода, когда регион уже известен."""
        if self._view != _MODES:
            return
        widgets = list(self.query(".set-modes-hint"))
        if widgets:
            widgets[-1].update(self._modes_hint_text())

    def _modes_apply(self, direction: int) -> None:
        """←/→/Enter на строке тоггла (канон §1/§3): мгновенное применение — цикл
        значения ОТ ТЕКУЩЕГО отображаемого состояния + запись+hot-reload; ошибка
        записи — флеш «✗ не сохранено: …», подэкран остаётся открытым (не закрывается)."""
        from tui.toggles import apply_toggle, cycle_value, TOGGLES, toggle_state
        names = list(TOGGLES.keys())
        if not names:
            return
        name = names[self._modes_idx] if self._modes_idx < len(names) else names[0]
        current = toggle_state(self.app, name)
        new_value = cycle_value(name, current, direction)
        try:
            apply_toggle(self.app, name, new_value)
        except (ValueError, OSError) as exc:
            self._notice(f"✗ не сохранено: {exc}")
            return
        self._render_view()

    _UPDATE_ACTION_UPDATE = "Обновить агента"
    _UPDATE_ACTION_CHECK = "Проверить обновления"
    _UPDATE_ACTION_SYNC = "Обновить config.yml до текущей версии"

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
        dev = src is None or src.editable
        # DI через _source: app_version.install_source привязан late-binding'ом к
        # моменту импорта updater.source — в тестах monkeypatch этого не увидит.
        resolved = None if dev else resolved_source(_source=lambda: src)
        ver = Text()
        ver.append("Версия     ", style=DEEP_DIM)
        if dev:
            ver.append("dev · установка из исходников", style=DIM)
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
        actions = [self._UPDATE_ACTION_SYNC] if dev else [
            self._UPDATE_ACTION_UPDATE, self._UPDATE_ACTION_CHECK, self._UPDATE_ACTION_SYNC]
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
        return ("Перепишет файл по образцу текущей версии, ваши значения сохранятся, "
                "старый файл — в бэкап. На работу агента не влияет.")

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
        update идемпотентен и не зависит от повторных нажатий."""
        try:
            w = self.query_one(f"#{wid}", Static)
        except NoMatches:
            self.query_one("#set-frame", Vertical).mount(
                Static(text, id=wid, classes=cls))
            return
        w.update(text)
        w.set_classes(cls)

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
            from tui.editors.connection import ConnectionEditor
            url, header, error, proxy = self.query_one(ConnectionEditor).submit()
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
            from config_origin import OVERLAY_LABELS
            origin = self._origin(overridden[0])
            source = OVERLAY_LABELS[origin.overlay_kind]
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

    def _open_section(self, i: int) -> None:
        self._view = i
        self._reset_draft_state()   # бампает gen — стухший воркер прежней секции отбрасывается
        if i == _UPDATE:
            self._update_idx = None     # пересчитать дефолт-курсор при (пере)входе в секцию
        self._render_view()
        # Авто-проверка при ПЕРВОМ входе в «Обновление» за сессию (как probe в др. секциях):
        # секция сразу показывает статус, а не «не проверялось». Кэш (_update_check_result)
        # переживает повторные входы → сеть не долбим; «Проверить обновления» форсит свежий.
        # На dev-установке _check_updates_now — no-op (источника обновления нет).
        if i == _UPDATE and self._update_check_result is None and not self._update_checking:
            self._check_updates_now()

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
            if self._view in _EDITOR_SECTIONS and self._pending_env_save is not None:
                event.stop()
                self._cancel_env_confirm()     # отмена подтверждения, из секции не выходим
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
        if event.key == "tab" and self._view in _PROBE_SECTIONS:
            event.stop()
            event.prevent_default()                   # не отдавать focus-next
            if not self._writable():
                return                      # read-only: редактора нет, проверять нечего
            self._reset_draft_state()
            self._start_draft_probe(self._view)
            return
        if event.key == "enter" and self._view in _PROBE_SECTIONS:
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
            if self._draft_checked is not None:       # уже проверено (Tab): решаем по вердикту
                self._smart_enter_decide(section, values, self._draft_checked)
                return
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
        if event.key in ("up", "down") and self._view == _MODES:
            event.stop()
            from tui.toggles import TOGGLES
            n = len(TOGGLES)
            if n > 1:
                self._modes_idx = (self._modes_idx + (1 if event.key == "down" else -1)) % n
                self._render_view()
            return
        if event.key in ("left", "right", "enter") and self._view == _MODES:
            event.stop()
            self._modes_apply(-1 if event.key == "left" else 1)
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
                if event.key == "ctrl+d":
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
                    from tui.editors.connection import ConnectionEditor
                    ed = self.query_one(ConnectionEditor)
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
                ed.move(-1 if event.key == "up" else 1)
                self._reset_draft_state()
                self._update_dirty()
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
        if self._view in _EDITOR_SECTIONS:
            self._update_dirty()
