"""Нативный Textual-онбординг gc-ua (презентация + оркестрация шагов).

Списки-с-выбором — hand-rolled Static-карточки + on_key (идиома McpScreen).
Переиспользует ядро: onboarding (политика), onboarding_config_writer,
model_picker, mcp_runtime.cli. Логика сбора — как в плоском onboarding_wizard,
меняется только презентация."""
from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Static
from rich.text import Text

import onboarding
from tui import branding
from tui.palette import ACCENT, DEEP_DIM, DIM, INK, OK, WARN
from tui.form_frame import cap_frame_height

# Именованные шаги мастера. Состав статичен, но шаг адресуется ИМЕНЕМ, а не
# номером: числовые сравнения врали бы при любой правке состава.
STEP_CONNECTION = "connection"
STEP_MODEL = "model"
STEP_LEVELS = "levels"
STEP_MCP = "mcp"
STEP_CONFIRM = "confirm"

_BASE_STEPS = (STEP_CONNECTION, STEP_MODEL, STEP_LEVELS, STEP_MCP, STEP_CONFIRM)
_STEP_TITLE = {
    STEP_CONNECTION: "Подключение",
    STEP_MODEL: "Модель",
    STEP_LEVELS: "Уровни моделей",
    STEP_MCP: "MCP-серверы",
    STEP_CONFIRM: "Подтверждение",
}
_STEP_SHORT = {                    # короткие имена для степпера (финала в нём нет)
    STEP_CONNECTION: "Подключение",
    STEP_MODEL: "Модель",
    STEP_LEVELS: "Уровни",
    STEP_MCP: "MCP",
}
_MARK_STEPS = (STEP_MODEL, STEP_LEVELS)   # шаги с отдельной от курсора пометкой ● (двухролевой Enter)

# Шаг уровней (спека §11: «добавить лёгкую и максимальную модели?») — две
# независимые под-формы, каждая необязательна: пропуск не пишет ничего.
_LEVEL_ROWS = ("light", "max")
_LEVELS_QUESTION = ("Добавить лёгкую и максимальную модели? Шаг можно пропустить — "
                    "уровни настраиваются и позже, в настройках.")
_LEVEL_UNSET = "не задана"
# Шаг «Модель» задаёт тот же уровень, что и шаг «Уровни», — `default` (одна карта
# `MODEL_SLOT_KEYS` на все три, один путь записи). Разведены они только по
# обязательности: без `default` конфиг нерабочий. Подпись называет эту связь — иначе
# слово «default» человек впервые встречает уже в итоговой сводке.
_MODEL_STEP_HINT = ("основная модель — уровень default; light и max можно задать "
                    "на следующем шаге")
# Выход со шага строкой-действием, а не клавишей: Enter на этом шаге раскрывает форму
# уровня под курсором, и «далее» ему взять неоткуда — курсор всегда стоит на уровне.
# Идиома строки-действия — из секции «Модели» (`models_section._ADD_ROW_LABEL`):
# глиф + подпись, приглушённо. Шаг необязательный, поэтому подпись одна на оба исхода
# (уровни заданы / пропущены) — скачущее имя рассказывало бы про механику, не про выбор.
_LEVELS_CONTINUE_LABEL = "→ продолжить"
_LEVEL_FORM_LABEL = {"light": "Лёгкая модель (light)",
                     "max": "Максимальная модель (max)"}

# Раскрытый список забирает клавиши себе — футер на это время становится его: обещать
# «Enter — далее», когда Enter подставляет строку списка, значит врать (спека §6.4).
# Пресет-лист адреса на шаге «Подключение» — список раскрывается САМ на входе в поле,
# то есть человек застаёт это состояние по умолчанию, а не ищет его: список раскрыт
# сразу, Enter подставляет провайдера и возвращается, Esc сворачивает список, а не
# выходит из мастера. Ctrl+Space до списка не относится и работает как везде на шаге —
# проверяет связь.
_FOOTER_PRESET_LIST = ("↑↓ — выбор провайдера  ·  Enter — подставить  ·  Esc — свернуть"
                       "  ·  Ctrl+Space — проверить связь  ·  PgDn — далее")
# Набран свой адрес — в списке не осталось строк: подставлять нечего, Enter лишь
# сворачивает список, оставляя набранное.
_FOOTER_PRESET_LIST_NO_MATCH = ("Enter — оставить свой адрес  ·  Esc — свернуть"
                                "  ·  Ctrl+Space — проверить связь  ·  PgDn — далее")


class OnboardingScreen(Screen):
    # Модальный экран: конфига ещё нет, глобальная навигация (F1/F4/…) поверх мастера
    # показывала бы справку с неработающими клавишами и уведомления по незаписанному
    # конфигу. Гейт — TuiApp.check_action.
    BLOCKS_GLOBAL_NAV = True

    # Хвост плашки о перекрывающем профиле: экран мастера — единственная поверхность,
    # где «Настройки · Профили» существуют, поэтому только он вправе звать туда.
    _NOTE_SETTINGS_TAIL = " · изменить в Настройки · Профили"

    # priority=True — перехватывает PgUp/PgDn раньше фокусированного Input (иначе поля
    # #conn-url/#conn-token/#onb-model не отдали бы клавишу навигации).
    BINDINGS = [
        Binding("pageup", "go_back", "назад", show=False, priority=True),
        Binding("pagedown", "go_next", "далее", show=False, priority=True),
    ]

    def __init__(self, config, config_path, *, review_all, on_commit,
                 fetch=None, mcp=None, probe=None) -> None:
        super().__init__()
        self._config = config
        self._config_path = config_path
        # Хранится ради теста на wiring (app.py передаёт флаг, но сам его не читает) —
        # сами гейты на review_all внутри экрана сняты (сырой документ различает
        # «первичный запуск / уже настроен» сам по себе, см. _raw_conn), поэтому
        # мастер больше не ведёт себя по-разному в review- и auto-режиме.
        self._review_all = review_all
        self._on_commit = on_commit
        from model_picker import fetch_models
        from tui.services.model_list_cache import make_cached_fetch
        self._fetch = make_cached_fetch(fetch or fetch_models)
        if mcp is None:
            import mcp_runtime.cli as mcp_mod
            self._mcp = mcp_mod
        else:
            self._mcp = mcp
        from tui.services.settings_probe import probe as _default_probe
        self._probe_fn = probe or _default_probe
        self._steps = _BASE_STEPS
        self._step = 0
        self._levels_stage = "ask"     # "ask" (две строки) | "light" | "max" (под-форма)
        self._levels_idx = 0
        self._values: dict = {}
        self._idx: dict = {}
        self._mcp_selected: set = set()
        self._mcp_installed: set = set()   # уже подключённые (wired) — отмечаем, не предлагаем
        self._probe_gen = 0   # generation авто-пробы шага «Подключение»: протухший результат отбрасывается
        self._last_probed_snapshot: tuple | None = None   # (url, token_raw, proxy) последней пробы — дребезг

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            with Horizontal(id="onb-header"):
                yield Static(self._title_text(), id="header-left", markup=False)
            scroll = VerticalScroll(id="onb-scroll")
            scroll.can_focus = False
            with scroll:
                with Vertical(id="onb-frame"):
                    yield Static(branding.logo_text(), id="onb-logo", markup=False)
                    yield Static(branding.tagline_under_logo(), id="onb-tagline", markup=False)
                    yield Static(self._stepper_text(), id="onb-stepper", markup=False)
                    yield Static(self._summary_text(), id="onb-summary",
                                 classes="onb-hint", markup=False)
                    profile_note = self._layer_note()
                    if profile_note:
                        yield Static(profile_note, id="onb-profile-warn",
                                     classes="onb-warn", markup=False)
                    yield Static(self._body_placeholder(), id="onb-body", markup=False)
            yield Static(self._footer_text(), id="onb-footer", markup=False)

    # --- плашка о перекрывающем слое ---
    def _layer_note(self):
        """Вердикт `overriding_layer_note` по конфигу запуска: профилями мастер не
        управляет (это «Настройки · Профили»), поэтому его шаги вердикт не меняют —
        плашка ставится раз в `compose` и переживает пересборку тела шага (та сносит
        только `.onb-dyn`).

        Общий текст политики — факт без адреса (его печатает и gc-cli); адрес
        действия дописывает поверхность, и только профильному вердикту: экран
        «Настройки · Профили» правит профиль, а не чужой слой реестра."""
        config = self._config or {}
        note = onboarding.overriding_layer_note(config, self._config_path)
        if note and config.get("profile"):
            note += self._NOTE_SETTINGS_TAIL
        return note

    # --- состав шагов (имена вместо номеров) ---
    def _step_id(self) -> str:
        """Имя шага под курсором — им и меряются все развилки экрана."""
        return self._steps[self._step]

    def _goto(self, step_id: str) -> None:
        """Перейти на именованный шаг. Прод-переходы идут через `_advance`/`_go_back`;
        сегодня это тест-хелпер — тесты навигации адресуют шаги им."""
        self._leave_step()
        self._step = self._steps.index(step_id)
        self._render_step()

    def _leave_step(self) -> None:
        """Уход со шага — общий хвост всех переходов: летящая авто-проба
        обесценивается, а под-состояние шага (под-форма уровня) его не переживает.
        Иначе вернувшийся человек застаёт шаг в середине чужого выбора."""
        self._probe_gen += 1
        self._levels_stage = "ask"

    def _last_step(self) -> int:
        """Позиция финального шага — ПО ИМЕНИ. «Последний по индексу» — то самое
        позиционное допущение, из-за которого состав шагов и переехал на имена."""
        return self._steps.index(STEP_CONFIRM)

    # --- хром (хедер-титул + именованный степпер + футер-подсказки, как основной экран) ---
    def _title_text(self) -> str:
        if self._step_id() == STEP_CONFIRM:
            return "Настройка · подтверждение"
        return f"Настройка · {self._step + 1}/{self._last_step()}"

    def _stepper_text(self) -> Text:
        t = Text()
        for i, name in enumerate(_STEP_SHORT[step] for step in self._steps[:self._last_step()]):
            if i:
                t.append("    ")
            if i < self._step:
                mark, style = "✓ ", OK             # пройденные — зелёные (на финале все шаги пройдены)
            elif i == self._step:
                mark, style = "● ", INK            # текущий — белый (как выбранная строка)
            else:
                mark, style = "○ ", DEEP_DIM
            t.append(mark, style=style)
            t.append(name, style=f"bold {style}" if i == self._step else style)
        return t

    def _shown_values(self) -> dict:
        """Чем витрины мастера (лента-сводка и подтверждение) заполняют то, чего
        человек сам не набрал, — резолвнутым конфигом запуска. При активном профиле
        это значения его слоя: витрина говорит о том, чем агент реально будет
        работать, а плашка рядом объясняет, откуда они (спека §5)."""
        return {"completions_url": self._config.get("completions_url"),
                "model": self._config.get("model"),
                "Authorization": (self._config.get("chat_completion_headers") or {}
                                  ).get("Authorization")}

    def _summary_text(self) -> str:
        from tui.services.onboarding_summary import summary_line
        shown = self._shown_values()
        # Шаблонные заглушки конфига (…_SENTINEL) — это «ещё не задано», а не значение:
        # показываем прочерком, как редактор адреса трактует URL_SENTINEL пустым полем.
        url = self._values.get("completions_url") or shown.get("completions_url") or ""
        if url == onboarding.URL_SENTINEL:
            url = ""
        model = self._values.get("model") or shown.get("model") or ""
        if model == onboarding.MODEL_SENTINEL:
            model = ""
        # Токен для сводки — только «задан / не задан / без авторизации» (mask_authorization
        # сюда не идёт), поэтому фолбэк безопасен: секрет никогда не показывается
        # литералом, только Text._token'ом ("Bearer"/"—"/"нет"), и в файл эта строка
        # не пишется (см. _submit_connection про запись).
        auth = self._values.get("Authorization")
        if auth is None:
            auth = shown.get("Authorization")
        if auth == onboarding.TOKEN_SENTINEL:
            auth = None
        return summary_line(
            url=url,
            auth=auth,
            model=model,
            mcp_count=len(self._mcp_selected),
        )

    def _footer_text(self) -> str:
        step = self._step_id()
        if step == STEP_CONFIRM:
            return "Enter — подтвердить и завершить настройку  ·  PgUp — назад  ·  Esc — выход"
        if step == STEP_CONNECTION and self._presets_open():
            return (_FOOTER_PRESET_LIST if self._presets_have_selection()
                    else _FOOTER_PRESET_LIST_NO_MATCH)
        parts = ["↑↓ навигация", "Enter — далее", "Esc — выход"]
        # Enter ведёт далее не везде: на помеченных шагах он ставит пометку под
        # курсором. «Далее» в эти моменты — PgDn.
        enter_chooses = (step in _MARK_STEPS and not self._step_marked()
                         # …кроме строки-действия списка уровней: там Enter и есть «далее».
                         and not (step == STEP_LEVELS and self._levels_stage == "ask"
                                  and self._levels_on_continue()))
        if enter_chooses:
            parts[1:2] = ["Enter — выбрать", "PgDn — далее"]
        # Развод §2 спеки 2026-08-06: раскрытие/перечитывание списка ушло на Alt+↓,
        # Ctrl+Space осталась только там, где есть проба (карта §2.5) — на шаге
        # «Подключение» она проверяет связь, на «Модели» пробы нет вовсе и клавиша в
        # футере не обещается (§5.1 спеки). На шаге «Подключение» список свёрнут ровно
        # в этой ветке (раскрытый уходит футером выше) — тогда футер обязан назвать
        # способ его вернуть, иначе `Esc` увёл бы список без пути назад.
        if step == STEP_CONNECTION:
            parts.insert(1, "Ctrl+Space — проверить связь")
            if self._connection_on_url():
                # Alt+↓ раскрывает список только с адреса (`reload_presets` гейтит
                # ровно эту позицию курсора) — на «Токене»/«Прокси» клавиша молчит, и
                # называть её там значило бы обещать неработающее (находка финального
                # ревью 2026-08-06, D).
                parts.insert(1, "Alt+↓ — показать список")
        elif step == STEP_MODEL or self._level_form_open():
            # Под-форма уровня — тот же редактор со списком с сервера, что шаг
            # «Модель», и та же клавиша перечитывания.
            parts.insert(1, "Alt+↓ — перечитать список")
        if self._level_form_open():
            # Под-состояние внутри шага: Esc закрывает ЕГО и возвращает к списку
            # уровней — обещать «выход» здесь значило бы врать (Esc-вложенность).
            parts[-1] = "Esc — назад"
        if self._step > 0:
            parts.insert(1, "PgUp — назад")
        if step == STEP_MCP:
            # MCP: Space — тумблер чекбоксов, своя роль, и подсказка идёт первой.
            parts.insert(0, "Space — выбрать")
        return "  ·  ".join(parts)

    def _connection_editor(self):
        """Редактор шага «Подключение» или None. Отдельный доступ нужен футеру: тот
        считается и из `compose`, где тела шага ещё нет вовсе."""
        from tui.editors.connection import ConnectionEditor
        eds = self.query(ConnectionEditor)
        return eds.first() if eds else None

    def _presets_open(self) -> bool:
        ed = self._connection_editor()
        return ed is not None and ed.presets_open

    def _presets_have_selection(self) -> bool:
        ed = self._connection_editor()
        return ed is not None and ed.presets_have_selection

    def _level_form_open(self) -> bool:
        """Шаг уровней раскрыт в под-форму выбора модели (не список уровней)."""
        return self._step_id() == STEP_LEVELS and self._levels_stage != "ask"

    def _connection_on_url(self) -> bool:
        """Курсор шага «Подключение» — на поле адреса. Редактор ещё не смонтирован
        (самый первый кадр шага) — курсор по умолчанию там (`ConnectionEditor._idx`
        стартует с 0 = адрес), поэтому `None` тоже читаем как «да»."""
        ed = self._connection_editor()
        return ed is None or ed.on_url_field

    def _step_marked(self) -> bool:
        """Помечена ли ● строка под курсором текущего шага. От этого зависит роль Enter
        (выбрать vs далее) и текст футера; шаги без пометки (Подключение, MCP) — False.

        Список уровней пометки не знает вовсе: Enter там всегда открывает под-форму
        уровня, «далее» со шага уводит PgDn."""
        from textual.css.query import NoMatches
        step = self._step_id()
        if step == STEP_LEVELS and self._levels_stage == "ask":
            return False
        try:
            if step in (STEP_MODEL, STEP_LEVELS):
                from tui.editors.model import ModelEditor
                return self.query_one(ModelEditor).is_marked
        except NoMatches:
            return False                 # тело шага ещё не смонтировано
        return False

    def on_connection_editor_ready(self, event) -> None:
        """То же, что в `/settings`: редактор адреса домонтировался и решил судьбу
        пресет-листа — футер пересчитывается только теперь. Оба расчёта
        `_mount_step_body` идут раньше `on_mount` редактора, и первый кадр шага
        обещал бы клавиши без списка."""
        event.stop()
        self._refresh_footer()

    def _refresh_footer(self) -> None:
        """Точечно обновить футер (роль Enter меняется от пометки под курсором) —
        без пересбора тела шага, иначе набранное в полях терялось бы."""
        self.query_one("#onb-footer", Static).update(self._footer_text())

    def _refresh_chrome(self) -> None:
        """Хром после пометки: значение уже зафиксировано — сводка и футер догоняют."""
        self.query_one("#onb-summary", Static).update(self._summary_text())
        self._refresh_footer()

    def _body_placeholder(self) -> str:
        return _STEP_TITLE[self._step_id()]

    def on_mount(self) -> None:
        self._mount_step_body()
        self.call_after_refresh(cap_frame_height, self, "#onb-scroll")

    def on_resize(self, event: events.Resize) -> None:
        # Потолок рамки зависит только от высоты окна и хрома — пересчитывается на
        # ресайз, а не на каждый кадр шага (см. form_frame).
        cap_frame_height(self, "#onb-scroll")

    def _mount_step_body(self) -> None:
        """Смонтировать виджеты текущего шага в #onb-frame (после #onb-body)."""
        # Снять фокус ДО remove: detached focused-виджет рвёт Screen._binding_chain
        # (идёт через focused.ancestors_with_self) — priority PgUp перестаёт находиться.
        self.set_focus(None)
        for w in self.query(".onb-dyn"):
            w.remove()
        body = self.query_one("#onb-frame", Vertical)
        step = self._step_id()
        if step == STEP_CONNECTION:
            from tui.editors.connection import ConnectionEditor
            from config_modules.endpoint_presets import endpoint_presets_from_config
            # Поля показывают СЫРОЕ значение файла, не self._config (он уже резолвнут
            # ${ENV:...}/${FILE:...}) — см. _raw_conn.
            raw = self._raw_conn()
            cur = self._values.get("completions_url")
            if not cur:
                raw_url = raw.get("completions_url")
                cur = raw_url if raw_url and raw_url != onboarding.URL_SENTINEL else ""
            auth = self._values.get("Authorization")
            if auth is None:
                auth = raw.get("Authorization")
            proxy_cur = self._values.get("proxy")
            if proxy_cur is None:
                proxy_cur = raw.get("proxy") or ""
            ed = ConnectionEditor(
                url_current=cur, auth_header=auth,
                proxy_current=proxy_cur or "",
                create_mode=True,
                presets=endpoint_presets_from_config(self._config or {}))
            body.mount(ed)
            body.mount(Static(Text("— не проверено", style=DEEP_DIM),
                              classes="onb-hint onb-dyn set-group-gap onb-conn-probe"))
        elif step == STEP_MODEL:
            from tui.editors.model import ModelEditor
            body.mount(Static(_MODEL_STEP_HINT, classes="onb-hint onb-dyn set-spaced",
                              markup=False))
            endpoint, auth = self._endpoint_auth()
            cfg_model = self._config.get("model") or ""
            if cfg_model == onboarding.MODEL_SENTINEL:
                cfg_model = ""
            ed = ModelEditor(current=self._values.get("model", ""), endpoint=endpoint,
                             auth=auth, verify=bool(self._config.get("use_ssl", True)),
                             fetch=self._fetch,
                             # Список приезжает воркером: до него метить нечего, после —
                             # курсор встаёт на помеченную. Футер обещает роль Enter — догоняем.
                             on_loaded=self._refresh_footer,
                             active=self._values.get("model") or cfg_model,
                             # Набранное на шаге «Подключение» (self._values["proxy"]),
                             # фолбэк — резолвнутый self._config (см. _effective_proxy):
                             # без него запрос за списком в корп-сети с обязательным
                             # прокси идёт мимо него, и «список не получен» здесь
                             # противоречит «✓ … · N моделей» с шага «Подключение».
                             config_proxy=self._effective_proxy() or None)
            body.mount(ed)
        elif step == STEP_LEVELS:
            self._mount_levels_body(body)
        elif step == STEP_MCP:
            self._idx.setdefault(STEP_MCP, 0)
            names = list(self._mcp.installable_names())
            self._mcp_names = names
            try:
                self._mcp_desc = self._mcp.descriptions()
            except Exception:
                self._mcp_desc = {}
            try:
                self._mcp_installed = set(self._mcp.wired_names())
            except Exception:
                self._mcp_installed = set()
            cur = self._idx[STEP_MCP]
            for i, name in enumerate(names):
                cls = "onb-option onb-dyn set-spaced onb-mcp-row" + (" selected" if i == cur else "")
                body.mount(Static(self._mcp_row_text(i == cur, name), classes=cls))
        elif step == STEP_CONFIRM:
            from onboarding import mask_authorization
            shown = self._shown_values()
            addr = self._values.get("completions_url") or shown.get("completions_url") or "—"
            model = self._values.get("model") or shown.get("model") or "—"
            # Плейсхолдеры шаблона — не значения (тот же канон, что в строке-сводке
            # выше и в витринах базы): нетронутое поле в _values не попадает, а
            # `agent.model` мастер с волны B не пишет вовсе — у вернувшегося в мастер
            # человека оба плейсхолдера так и лежат в конфиге.
            if addr == onboarding.URL_SENTINEL:
                addr = "—"
            if model == onboarding.MODEL_SENTINEL:
                model = "—"
            # Нетронутое поле токена в _values не попадает (см. _submit_connection) —
            # фолбэк для отображения безопасен: строка тут только маскируется
            # (mask_authorization), в файл эта ветка не пишет.
            auth = self._values.get("Authorization")
            if auth is None:
                auth = shown.get("Authorization")
            if auth is None or auth == onboarding.TOKEN_SENTINEL:
                tok = "—"
            elif auth == "":
                tok = "без авторизации"
            else:
                tok = mask_authorization(auth)
            mcp_val = (", ".join(sorted(self._mcp_selected)) if self._mcp_selected
                       else "не выбраны — можно добавить позже")
            # Слоты реестра — сущности одного рода, и названы одинаково: «модель» держит
            # контекст, имя слота — то же, что уедет в файл (`slots.default`) и что
            # человек увидит в секции «Модели» и в `/level`.
            rows = [("адрес:", addr), ("модель default:", model)]
            # Строки необязательных шагов — только когда на них что-то решено:
            # подтверждают ровно то, что уедет в файл.
            for level in _LEVEL_ROWS:
                value = self._values.get(onboarding.MODEL_SLOT_KEYS[level])
                if value:
                    rows.append((f"модель {level}:", value))
            rows += [("токен:", tok), ("MCP:", mcp_val)]
            summary = Text()
            label_width = max(len(label) for label, _ in rows) + 1  # колонка под самую длинную метку
            for n, (label, val) in enumerate(rows):
                if n:
                    summary.append("\n")            # воздух между строками сводки
                # Галочка — про ЗАПОЛНЕННОЕ: «✓ адрес: —» читается «готово» там, где
                # ничего нет. Глиф гасим, а не убираем — колонка меток не съезжает.
                summary.append("  " if val == "—" else "✓ ", style=OK)
                summary.append(f"{label:{label_width}}", style=DIM)
                summary.append(f"{val}\n", style=INK)
            notice_parts = []
            if self._mcp_selected:
                notice_parts.append(f"MCP ({mcp_val})")
            if notice_parts:
                summary.append("\n")                # воздух перед служебной строкой
                summary.append("Установятся после подтверждения: " + " · ".join(notice_parts),
                               style=DEEP_DIM)
            body.mount(Static(summary, classes="onb-dyn"))
        # Футер — про тело, поэтому считается ПОСЛЕ него, а не вместе с остальным хромом:
        # состав тела задаёт и состав обещаний (группы для Tab, раскрытый список).
        self._refresh_footer()
        # И ещё раз, когда дерево устоялось: дети настраивают себя в СВОЁМ on_mount
        # (ConnectionEditor оттуда же фокусирует адрес и раскрывает пресет-лист) — это
        # уже после вызова выше, и первый кадр шага обещал бы клавиши без списка.
        self.call_after_refresh(self._refresh_footer)

    def _render_step(self) -> None:
        """Перерисовать хром (хедер/степпер) + тело под текущий _step; футер идёт
        последним, внутри `_mount_step_body`."""
        self.query_one("#header-left", Static).update(self._title_text())
        self.query_one("#onb-stepper", Static).update(self._stepper_text())
        self.query_one("#onb-summary", Static).update(self._summary_text())
        self.query_one("#onb-body", Static).update(self._body_placeholder())
        # Футер здесь НЕ обновляем: он описывает тело, которого ещё нет. Посчитанный
        # до монтажа, он видит одну группу вместо двух и молчит про Tab ровно там, где
        # тот нужен, — «поля вижу, войти нечем». Его обновляет сам `_mount_step_body`.
        self._mount_step_body()

    def _submit_connection(self) -> None:
        """Шаг «Подключение»: три поля одной формы уезжают в _values разом — но только
        те, что реально тронуты (канон ConnectionEditor.submit(), тот же, что у /settings):
        нетронутое поле возвращает None и в _values не попадает, иначе значение из поля
        (прошедшее структурную валидацию адреса) уезжает обратно в файл при коммите и
        подменяет там ссылку ${ENV:...}/${FILE:...} её раскрытым значением — секрет
        открытым текстом в файле, который коммитят (поля префилятся СЫРЫМ значением
        файла, см. _raw_conn/_mount_step_body, а не self._config).

        Токен-поле — исключение из этой механики: create_mode не показывает переключатель
        «без авторизации» (Task 8), и пустое поле там неоднозначно само по себе — это либо
        «токена и не было» (честное «без авторизации»), либо «токен уже задан, просто не
        перепечатан» (секрет никогда не лежит в поле открытым текстом, канон ConnectionEditor).
        submit() этого не различает и на пустом поле в create_mode всегда отдаёт header=None —
        доразличаем сами по сырому значению файла/уже собранному в _values."""
        from tui.editors.connection import ConnectionEditor
        ed = self.query_one(ConnectionEditor)
        url, header, error, proxy = ed.submit()
        if error is not None:
            self._probe_gen += 1   # обесценить летящую пробу — не даём ей затереть ошибку
            self._set_conn_probe_line(Text(error, style=WARN))
            return
        _url, _auth_idx, token_raw, _proxy = ed.peek()
        if url is not None:
            self._values["completions_url"] = url
        if header is not None:
            self._values["Authorization"] = header
        elif not token_raw:
            raw = self._raw_conn()
            active = self._values.get("Authorization")
            if active is None:
                active = raw.get("Authorization")
            if not active or active == onboarding.TOKEN_SENTINEL:
                self._values["Authorization"] = ""   # честное «без авторизации» (Task 8)
            # иначе авторизация уже действует (в т.ч. ссылка ${ENV:...}/${FILE:...}), а
            # поле не тронуто — писать нечего, ссылка/секрет остаются как в файле.
        if proxy is not None:
            self._values["proxy"] = proxy
        self._advance()

    def _submit_model(self) -> None:
        """Enter на шаге модели — двухролевой: строка под курсором не помечена → Enter
        её фиксирует (● переезжает, шаг тот же); помечена (или помечать нечего) → далее."""
        from tui.editors.model import ModelEditor
        ed = self.query_one(ModelEditor)
        if ed.is_marked:                  # …в т.ч. «списка нет» — там метить не на чем
            self._advance_model()
            return
        value = ed.mark()
        if value is None:
            return       # ручной ввод пуст: помечать нечего — ждём ввода, шаг не прыгает
        self._values["model"] = value
        self._refresh_chrome()

    def _advance_model(self) -> None:
        from tui.editors.model import ModelEditor
        value = self.query_one(ModelEditor).submit()
        if value is not None:
            self._values["model"] = value
        self._advance()

    def _endpoint_auth(self) -> tuple:
        """Адрес и авторизация, известные мастеру ПРЯМО СЕЙЧАС: набранное на шаге
        «Подключение» первично, резолвнутый конфиг — фолбэк. Один источник на все
        шаги, которые ходят к серверу за списком моделей (шаг «Модель» и под-формы
        уровней): у них один и тот же эндпоинт, переспрашивать его нечего."""
        endpoint = (self._values.get("completions_url")
                    or (self._config or {}).get("completions_url") or "")
        auth = self._values.get("Authorization")
        if auth is None:
            auth = ((self._config or {}).get("chat_completion_headers") or {}).get("Authorization")
        if auth == onboarding.TOKEN_SENTINEL:
            auth = None
        return endpoint, auth

    # --- шаг уровней: две необязательные под-формы (light/max) ---
    def _mount_levels_body(self, body: Vertical) -> None:
        if self._levels_stage == "ask":
            # Воздух отделяет вопрос от строк-ответов, а сами строки идут вплотную —
            # грамматика блока настроек (`.set-hint` + `.set-row` без margin): плотный
            # список читается как один выбор, разреженный — как отдельные пункты.
            body.mount(Static(_LEVELS_QUESTION, classes="onb-hint onb-dyn set-spaced",
                              markup=False))
            for i, level in enumerate(_LEVEL_ROWS):
                cls = "onb-option onb-dyn" + (" selected" if i == self._levels_idx else "")
                body.mount(Static(self._level_row_text(i == self._levels_idx, level),
                                  classes=cls))
            cont = Text()
            cont.append("▸ " if self._levels_on_continue() else "  ", style=ACCENT)
            cont.append(_LEVELS_CONTINUE_LABEL, style=DIM)
            body.mount(Static(cont, classes="onb-option onb-dyn"
                              + (" selected" if self._levels_on_continue() else "")))
            return
        from tui.editors.model import ModelEditor
        level = self._levels_stage
        body.mount(Static(_LEVEL_FORM_LABEL[level], classes="onb-hint onb-dyn", markup=False))
        endpoint, auth = self._endpoint_auth()
        current = self._values.get(onboarding.MODEL_SLOT_KEYS[level], "")
        # Тот же редактор, что на шаге «Модель», со своим id поля (иначе два поля с
        # одним id в одном дереве). url/токен не переспрашиваются — записи реестра
        # наследуют их от плоского подключения (решение 10 плана).
        body.mount(ModelEditor(current=current, endpoint=endpoint, auth=auth,
                               verify=bool((self._config or {}).get("use_ssl", True)),
                               fetch=self._fetch, input_id="onb-level-model",
                               on_loaded=self._refresh_footer, active=current,
                               config_proxy=self._effective_proxy() or None))

    def _level_row_text(self, is_cursor: bool, level: str) -> Text:
        """Строка уровня — грамматика строк слотов секции «Модели»
        (`models_section._binding_row_text`): курсор, голова «уровень · роль»
        (копирайт §1 из единого источника), значение в своей колонке.

        Голова берётся у `slot_head_text` ЦЕЛИКОМ, вместе с раскраской: имя уровня
        несёт цвет уровня (`TOGGLES["level"]`) — тот же, каким его красят список
        `/level` и слоты секции «Модели». Собирать её здесь заново значило бы
        показывать одну сущность на одном экране двумя способами."""
        from tui.editors.models_section import slot_head, slot_head_text
        value = self._values.get(onboarding.MODEL_SLOT_KEYS[level], "")
        width = max(len(slot_head(name)) for name in _LEVEL_ROWS) + 2
        t = Text()
        t.append("▸ " if is_cursor else "  ", style=ACCENT)
        head = slot_head_text(level)
        head.pad_right(max(0, width - len(head.plain)))
        t.append(head)
        t.append(value or _LEVEL_UNSET, style=DIM if value else DEEP_DIM)
        return t

    def _levels_on_continue(self) -> bool:
        """Курсор списка уровней — на строке-действии (последней), а не на уровне."""
        return self._levels_idx == len(_LEVEL_ROWS)

    def _move_levels(self, delta: int) -> None:
        # Строк на одну больше, чем уровней: последняя — «продолжить».
        self._levels_idx = (self._levels_idx + delta) % (len(_LEVEL_ROWS) + 1)
        self._render_step()

    def _submit_levels(self) -> None:
        """Enter шага: в списке — открыть под-форму уровня под курсором; в под-форме —
        та же двухтактная механика, что на шаге «Модель» (не помечено → Enter метит,
        помечено → фиксирует и возвращает к списку)."""
        if self._levels_stage == "ask":
            if self._levels_on_continue():
                self._advance()
                return
            self._levels_stage = _LEVEL_ROWS[self._levels_idx]
            self._render_step()
            return
        from tui.editors.model import ModelEditor
        ed = self.query_one(ModelEditor)
        if ed.is_marked:                  # …в т.ч. «списка нет» — там метить не на чем
            self._close_level_form(take_value=True)
            return
        value = ed.mark()
        if value is None:
            return       # ручной ввод пуст: помечать нечего — ждём ввода, форма стоит
        self._values[onboarding.MODEL_SLOT_KEYS[self._levels_stage]] = value
        self._refresh_footer()

    def _close_level_form(self, *, take_value: bool) -> None:
        """Вернуться из под-формы к списку уровней. `take_value` — закрытие «далее»
        (значение под курсором уезжает в `_values`, как `_advance_model` на шаге
        «Модель»); Esc закрывает без него: уровень необязателен, и отказ обязан
        оставаться отказом."""
        if take_value:
            from tui.editors.model import ModelEditor
            value = self.query_one(ModelEditor).submit()
            if value:
                self._values[onboarding.MODEL_SLOT_KEYS[self._levels_stage]] = value
        self._levels_stage = "ask"
        self._render_step()

    _DESC_COL = 50   # то же значение, что McpSectionEditor._row_text в /settings (согласованность)

    def _mcp_row_text(self, is_cursor: bool, name: str) -> Text:
        """Строка MCP: курсор ▸ + чекбокс/статус + имя + приглушённое описание (тот же
        источник, что /settings). Уже подключённые (wired в config.local.yml) — [✓] «подключён» (тускло)."""
        t = Text()
        t.append("▸ " if is_cursor else "  ", style=ACCENT)
        desc = self._mcp_desc.get(name, "") if hasattr(self, "_mcp_desc") else ""
        if name in self._mcp_installed:
            t.append("[✓] ", style=OK)
            t.append(name, style=DEEP_DIM)
            t.append("  подключён", style=DEEP_DIM)
            self._append_desc(t, desc)
            return t
        # Чекбокс (множественный выбор): [●] включён — белый кружок, единый язык «●=активно».
        t.append("[")
        if name in self._mcp_selected:
            t.append("●", style=INK)
        else:
            t.append(" ")
        t.append(f"] {name}")
        self._append_desc(t, desc)
        return t

    def _append_desc(self, t: Text, desc: str) -> None:
        """Дописать приглушённое описание с выравниванием на колонку (приём — как
        McpSectionEditor._row_text в /settings)."""
        if not desc:
            return
        pad = self._DESC_COL - t.cell_len
        t.append(" " * pad if pad > 0 else "  ")
        t.append(desc, style=DEEP_DIM)

    def _move_mcp(self, delta: int) -> None:
        names = getattr(self, "_mcp_names", [])
        n = max(1, len(names))
        self._idx[STEP_MCP] = (self._idx.get(STEP_MCP, 0) + delta) % n
        cur = self._idx[STEP_MCP]
        opts = list(self.query(".onb-option"))
        for i, w in enumerate(opts):
            w.update(self._mcp_row_text(i == cur, names[i]))
            if i == cur:
                w.add_class("selected")
            else:
                w.remove_class("selected")

    def _toggle_mcp(self) -> None:
        names = getattr(self, "_mcp_names", [])
        if not names:
            return
        cur = self._idx.get(STEP_MCP, 0)
        name = names[cur]
        if name in self._mcp_installed:
            return   # уже подключён — не переключаем
        if name in self._mcp_selected:
            self._mcp_selected.discard(name)
        else:
            self._mcp_selected.add(name)
        # update only the toggled widget in-place
        opts = list(self.query(".onb-option"))
        if cur < len(opts):
            opts[cur].update(self._mcp_row_text(True, name))

    def _raw_conn(self) -> dict:
        """Сырые (нерезолвнутые) значения полей шага «Подключение» ИЗ ФАЙЛА — тот же
        приём, что `SettingsScreen._read_raw`: self._config уже резолвнут
        (${ENV:...}/${FILE:...} подставлены), и показать или записать его значение
        обратно в файл значит подменить ссылку на переменную её значением — секрет
        открытым текстом в файле, который коммитят."""
        return self._raw_values("completions_url", "Authorization", "proxy")

    def _raw_values(self, *keys: str) -> dict:
        """{ключ-якорь: значение из базового файла как записано}. Файла нет — пусто."""
        if not self._config_path:
            return {}
        import config_raw
        from onboarding import FIELD_ANCHOR_PATHS
        values = config_raw.read_values(self._config_path, (FIELD_ANCHOR_PATHS[k] for k in keys))
        return {k: values[FIELD_ANCHOR_PATHS[k]] for k in keys}

    def _effective_proxy(self):
        """Прокси, известный мастеру ПРЯМО СЕЙЧАС — тот же приём, что праймит поле
        прокси на шаге «Подключение» (`_mount_step_body`, шаг 0): набранное на этом
        шаге (`self._values["proxy"]`) первично, конфиг — только фолбэк. Ключа нет
        вовсе (шаг ещё не проходили) — не то же самое, что пустая строка (прокси
        стёрли осознанно): фолбэк — ровно для первого случая, иначе только что
        снятый человеком прокси молча вернулся бы обратно из конфига."""
        proxy = self._values.get("proxy")
        if proxy is None:
            proxy = (self._config or {}).get("proxy")
        return proxy

    def _auto_probe(self) -> None:
        """Авто-проба шага «Подключение» (worker-поток): триггеры — подстановка
        пресета, уход с поля адреса/токена/прокси.

        Поле держит СЫРОЕ значение файла (см. _raw_conn/_mount_step_body) — если
        это ссылка ${ENV:...}/${FILE:...}, сама ссылка не адрес и не токен, а
        проба обязана стучаться в то, что реально уйдёт в запрос. Раскрытие —
        только для самого запроса (resolve_env_placeholders, тот же резолвер,
        что у config.load_config), причём внутри воркера: ${FILE:...} — синхронное
        чтение с диска, а не только резолв переменной, и в UI-потоке подвешивало бы
        интерфейс на каждый триггер (сетевой домашний каталог в корп-среде). В
        поле/_values/файл раскрытые значения не возвращаются — но текст сетевой
        ошибки в вердикте пробы может содержать раскрытый адрес.

        Токен-поле на нетронутом шаге принципиально пустое (секрет живёт только
        под маской placeholder, см. _mount_step_body) — фолбэк на действующий
        токен из self._config, тот же приём, что у соседнего шага «Модель»."""
        from tui.editors.connection import ConnectionEditor
        from textual.css.query import NoMatches
        try:
            ed = self.query_one(ConnectionEditor)
        except NoMatches:
            return
        url, _auth_idx, token_raw, proxy = ed.peek()
        if not url:
            return
        self._last_probed_snapshot = (url, token_raw, proxy)
        if onboarding.env_ref_status(url) == onboarding.ENV_REF_MISSING:
            # Резолв даст "" — стрелять пробой в пустой адрес нечестно: под
            # визуально заполненным полем проба соврала бы «нет соединения»/
            # «пустой адрес», как будто сломан сам мастер.
            self._probe_gen += 1   # обесценить летящую пробу
            self._set_conn_probe_line(Text(f"⚠ {onboarding.display_value(url)}", style=WARN))
            return
        auth = (onboarding.authorization_header(token_raw) if token_raw
                else self._values.get("Authorization"))
        if auth is None:
            auth = (self._config.get("chat_completion_headers") or {}).get("Authorization")
        if auth == onboarding.TOKEN_SENTINEL:
            auth = None
        verify = bool((self._config or {}).get("use_ssl", True))
        self._probe_gen += 1
        gen = self._probe_gen
        self._set_conn_probe_line(Text("проверяю…", style=DEEP_DIM))

        def _work() -> None:
            from config_modules.env_interpolation import resolve_env_placeholders
            from tui.services.settings_probe import ProbeResult
            probe_url = resolve_env_placeholders(url)
            probe_auth = resolve_env_placeholders(auth) if auth else auth
            probe_proxy = resolve_env_placeholders(proxy) if proxy else proxy
            config_proxy = probe_proxy or None
            try:
                result = self._probe_fn(probe_url, probe_auth, verify, "", config_proxy=config_proxy)
            except Exception as exc:
                result = ProbeResult("unknown", None, None, 0, f"внутренняя ошибка проверки: {exc}")
            self.app.call_from_thread(self._on_auto_probe_done, result, gen)

        self.run_worker(_work, thread=True, exclusive=True, name="onboarding-conn-probe")

    def _on_auto_probe_done(self, result, gen: int) -> None:
        if gen != self._probe_gen or not self.is_mounted:
            return                       # протухший результат (правка/переход обесценили)
        from tui.services.probe_presentation import probe_line_text
        self._set_conn_probe_line(probe_line_text(result))

    def _set_conn_probe_line(self, text) -> None:
        for w in self.query(".onb-conn-probe"):
            w.update(text)

    def _commit_config(self) -> bool:
        try:
            onboarding.write_wizard_values(self._config_path, self._values)
        except ValueError as exc:
            self.query_one("#onb-body", Static).update(f"✗ конфиг не записан: {exc}")
            return False
        self.query_one("#onb-body", Static).update("✓ конфиг записан")
        return True

    def _commit(self) -> None:
        if not self._commit_config():
            return
        self._install_selected()
        self._on_commit()

    def _install_selected(self) -> None:
        """Установки после подтверждения: MCP. Шумные subprocess'ы корёжат alt-screen —
        app.suspend() (прогресс в обычный терминал, затем TUI возвращается). Per-item
        try/except: одна упавшая установка не валит остальные и не блокирует переход."""
        names = sorted(self._mcp_selected)
        if not names:
            return

        def _do() -> None:
            for name in names:
                try:
                    self._mcp.install(name)
                except Exception:
                    pass

        try:
            with self.app.suspend():
                print("\nУстанавливаю MCP: " + ", ".join(names) + " …\n")
                _do()
        except Exception:
            _do()

    # --- навигация ---
    def _advance(self) -> None:
        if self._step < self._last_step():
            self._leave_step()
            self._step += 1
            self._render_step()

    def _go_back(self) -> None:
        """PgUp: сперва закрывается под-состояние шага (под-форма уровня), и только
        потом мастер отступает на шаг назад."""
        if self._level_form_open():
            self._close_level_form(take_value=False)
            return
        if self._step > 0:
            self._leave_step()
            self._step -= 1
            self._render_step()

    def action_go_back(self) -> None:
        self._go_back()

    def action_go_next(self) -> None:
        """PgDn — «далее» с любого места шага (зеркало PgUp): ровно та же валидация и
        тот же фолбэк «пометки нет → берём то, что под курсором», что у Enter-далее.
        Финал (подтверждение) подтверждают Enter'ом — навигационная клавиша конфиг не пишет."""
        step = self._step_id()
        if step == STEP_CONNECTION:
            self._submit_connection()
        elif step == STEP_MODEL:
            self._advance_model()
        elif self._level_form_open():
            self._close_level_form(take_value=True)   # «далее» из под-формы — к списку уровней
        elif step != STEP_CONFIRM:
            self._advance()

    def _confirm_exit(self) -> None:
        """Esc → модалка подтверждения выхода (как в основном TUI), не мгновенный выход."""
        from tui.screens.confirm import ConfirmScreen

        def _cb(ok) -> None:
            if ok:
                self.app.exit()

        self.app.push_screen(
            ConfirmScreen(message="Выйти из настройки? Введённые данные не сохранятся.",
                          hint="[Y] выйти  [N] остаться", title="Выход из настройки"),
            _cb,
        )

    def on_input_changed(self, event) -> None:
        """Печать меняет роль Enter: фильтр моделей пересобирает строки (курсор переезжает
        на якорь), ручной ввод — то, что зафиксирует пометка. Футер догоняет тем же
        хуком, что и ↑↓. Событие всплывает из редакторов — свои они уже обработали.

        Шаг «Подключение» здесь наравне с помеченными: набранный свой адрес выбивает из
        пресет-листа все строки, и Enter перестаёт подставлять."""
        if self._step_id() in _MARK_STEPS or self._step_id() == STEP_CONNECTION:
            self._refresh_footer()

    def on_key(self, event: events.Key) -> None:
        step = self._step_id()
        if event.key in ("alt+down", "ctrl+down"):
            # Развод клавиш (спека 2026-08-06 §2): раскрыть/перечитать список поля под
            # курсором. Списков в мастере два: пресеты адреса («Подключение») и модели
            # агента («Модель» и под-формы уровней — тот же редактор).
            if step == STEP_MODEL or self._level_form_open():
                # Поле со списком С СЕРВЕРА (§9.1 канона): свёртывания нет вовсе,
                # клавиша всегда уходит за списком заново.
                event.stop()
                from tui.editors.model import ModelEditor
                ed = self.query(ModelEditor)
                if ed:
                    ed.first().reload()
                return
            if step == STEP_CONNECTION:
                # Список пресетов адреса: свёрнутый раскрываем и клавишу съедаем,
                # раскрытый её не ест — перечитывать статику нечего (падает молча).
                from tui.editors.connection import ConnectionEditor as _ConnEd
                _ed = next(iter(self.query(_ConnEd)), None)
                if _ed is not None and _ed.reload_presets():
                    event.stop()
                    self._refresh_footer()   # клавиши теперь про список
                return
            return
        if event.key in ("ctrl+at", "ctrl+space", "ctrl+@"):
            # Развод §2 спеки 2026-08-06: клавиша больше не спрашивает список поля
            # (ушёл на Alt+↓ выше) — она либо проверяет связь (только шаг 0, карта проб
            # §2.5: на «Модели» пробы нет вовсе), либо съедается молча — не проваливается
            # в глобальные биндинги, но и не обещает того, чего на этих шагах нет
            # (§5.1 спеки).
            if step != STEP_CONNECTION:
                event.stop()
                return
            # Конец цепочки — «перепроверить связь». Зовём _auto_probe НАПРЯМУЮ, мимо
            # снимка `_last_probed_snapshot` (он живёт на местах вызова, а не внутри):
            # снимок гасит повтор автопробы на неизменных полях, и без ручного пути
            # вердикт «сервер недоступен» залипал бы навсегда (§5.2).
            event.stop()
            from textual.css.query import NoMatches
            from tui.editors.connection import ConnectionEditor
            try:
                url = self.query_one(ConnectionEditor).peek()[0]
            except NoMatches:
                return
            if not url:
                # _auto_probe на пустом адресе выходит ДО записи любой строки, и
                # клавиша осталась бы абсолютно немой. На чистой установке шаг
                # стартует именно с пустым адресом — там новичок и жмёт её первым
                # делом. Канон донора (`_start_draft_probe`): молчать в ответ на
                # клавишу нельзя, объясняем, чего не хватает.
                # В сам _auto_probe эта строка не идёт: для автопути (уход с поля,
                # подстановка пресета) молчание правильно.
                self._set_conn_probe_line(
                    Text("сначала настройте адрес — проверка стучится на него",
                         style=DEEP_DIM))
                return
            import config_raw
            if not onboarding._valid_endpoint_url(url) and not config_raw.env_refs(url):
                # §2.3: пока список раскрыт, в поле может лежать ФИЛЬТР, а не адрес —
                # бить пробой по нему вслепую нечестно (то же правило, что в
                # /settings «Подключение»).
                #
                # Ссылка ${ENV:VAR}/${FILE:...} — штатное СЫРОЕ значение поля (шаг
                # держит то, что лежит в файле, см. _mount_step_body): `_valid_endpoint_url`
                # бракует и её, но это не фильтр — блокировать пробу здесь значило бы
                # навсегда убить ручную перепроверку связи для корп-конфигов на
                # ${ENV:GC_URL} (находка ревью, фикс-раунд 1). `_auto_probe` уже умеет
                # честно ответить «переменной нет» (`env_ref_status`/`ENV_REF_MISSING`
                # внутри неё) — не дублируем эту развилку тут.
                self._set_conn_probe_line(
                    Text("сначала выберите адрес — проверка стучится на него",
                         style=DEEP_DIM))
                return
            self._auto_probe()
            return
        if event.key in ("tab", "shift+tab"):
            # Съедаем ВСЕГДА, даже когда переходить некуда (спека §4.2.6): tab/shift+tab —
            # штатные биндинги Textual (focus_next/focus_previous), и в мастере они явно
            # разрешены гейтом check_action в app.py. Оставить клавишу фреймворку значит
            # вернуть рассинхрон «фокус отдельно, курсор отдельно» — на шагах мастера
            # клавиша просто молчит.
            event.stop()
            return
        if event.key == "escape" and step == STEP_CONNECTION:
            # Esc при раскрытом пресет-листе сворачивает ТОЛЬКО его (канон /settings) —
            # общий Esc ниже (выход из мастера) в этом случае не должен стрелять.
            from tui.editors.connection import ConnectionEditor
            ed = self.query_one(ConnectionEditor)
            if ed.escape_presets():
                event.stop()
                self._refresh_footer()   # клавиши вернулись шагу — футер за ними
                return
        if event.key == "escape" and self._level_form_open():
            # То же правило вложенности для под-формы уровня: закрывается БЕЗ
            # значения — уровень необязателен, отказ обязан оставаться отказом.
            event.stop()
            self._close_level_form(take_value=False)
            return
        if event.key == "escape":
            event.stop()
            self._confirm_exit()
            return
        if event.key in ("up", "down") and step == STEP_LEVELS and self._levels_stage == "ask":
            event.stop()
            self._move_levels(-1 if event.key == "up" else 1)
            return
        if event.key in ("up", "down") and step == STEP_CONNECTION:
            # Единая лента курсора формы (канон ConnectionEditor.move из /settings):
            # адрес → токен → прокси; уход с поля токена/прокси/адреса — триггер
            # авто-пробы (спека §5.5).
            from tui.editors.connection import ConnectionEditor
            event.stop()
            ed = self.query_one(ConnectionEditor)
            leaving_id = getattr(self.focused, "id", None)
            ed.move(-1 if event.key == "up" else 1)
            if leaving_id in ("conn-token", "conn-proxy"):
                self._auto_probe()          # уход с поля токена/прокси — авто-проба
            elif leaving_id == "conn-url" and not ed.presets_open:
                url, _auth_idx, token_raw, proxy = ed.peek()
                if url and (url, token_raw, proxy) != self._last_probed_snapshot:
                    self._auto_probe()
            self._refresh_footer()   # вход на адрес раскрывает список, уход — сворачивает
            return
        if event.key in ("up", "down") and (step == STEP_MODEL or self._level_form_open()):
            from tui.editors.model import ModelEditor
            ed = self.query(ModelEditor)
            if ed and ed.first().models_loaded and ed.first().has_models:
                event.stop()
                ed.first().move(-1 if event.key == "up" else 1)
                self._refresh_footer()
                return
        if event.key in ("up", "down") and step == STEP_MCP:
            event.stop()
            self._move_mcp(-1 if event.key == "up" else 1)
            return
        if event.key == "space" and step == STEP_MCP:
            event.stop()
            self._toggle_mcp()
            return
        if event.key == "enter":
            event.stop()
            if step == STEP_CONNECTION:
                from tui.editors.connection import ConnectionEditor
                ed = self.query_one(ConnectionEditor)
                if ed.enter_presets():
                    # пресет-лист открыт — подставил/свернул, не сабмит; подстановка
                    # пресета — тоже триггер авто-пробы (спека §5.5).
                    url, _auth_idx, token_raw, proxy = ed.peek()
                    if url and (url, token_raw, proxy) != self._last_probed_snapshot:
                        self._auto_probe()
                    self._refresh_footer()   # список свернулся — футер снова про шаг
                    return
                self._submit_connection()
            elif step == STEP_MODEL:
                self._submit_model()
            elif step == STEP_LEVELS:
                self._submit_levels()
            elif step == STEP_CONFIRM:
                self._commit()
            else:
                self._advance()
