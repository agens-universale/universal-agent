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
import onboarding_fields as _of
from tui import branding
from tui.palette import ACCENT, DEEP_DIM, DIM, INK, OK, WARN
from tui.form_frame import cap_frame_height, reveal

# Именованные шаги мастера. Состав статичен, но шаг адресуется ИМЕНЕМ, а не
# номером: числовые сравнения врали бы при любой правке состава.
STEP_CONNECTION = "connection"
STEP_MODEL = "model"
STEP_LEVELS = "levels"
STEP_MCP = "mcp"
STEP_VOICE = "voice"
STEP_CONFIRM = "confirm"

_BASE_STEPS = (STEP_CONNECTION, STEP_MODEL, STEP_LEVELS, STEP_MCP, STEP_VOICE, STEP_CONFIRM)
_STEP_TITLE = {
    STEP_CONNECTION: "Подключение",
    STEP_MODEL: "Модель",
    STEP_LEVELS: _of.LEVELS_TITLE,
    STEP_MCP: "MCP-серверы",
    STEP_VOICE: "Голосовой ввод",
    STEP_CONFIRM: "Подтверждение",
}
_STEP_SHORT = {                    # короткие имена для степпера (финала в нём нет)
    STEP_CONNECTION: "Подключение",
    STEP_MODEL: "Модель",
    STEP_LEVELS: "Уровни",
    STEP_MCP: "MCP",
    STEP_VOICE: "Голосовой ввод",
}
_MARK_STEPS = (STEP_MODEL, STEP_LEVELS, STEP_VOICE)   # шаги с отдельной от курсора пометкой ● (двухролевой Enter)

# Шаг уровней (спека §11: «добавить лёгкую и максимальную модели?») — две
# независимые под-формы, каждая необязательна: пропуск не пишет ничего.
_LEVEL_ROWS = ("light", "max")
# Выход со шага строкой-действием, а не клавишей: Enter на этом шаге раскрывает форму
# уровня под курсором, и «далее» ему взять неоткуда — курсор всегда стоит на уровне.
# Идиома строки-действия — из секции «Модели» (`models_section._ADD_ROW_LABEL`):
# глиф + подпись, приглушённо. Шаг необязательный, поэтому подпись одна на оба исхода
# (уровни заданы / пропущены) — скачущее имя рассказывало бы про механику, не про выбор.
_LEVELS_CONTINUE_LABEL = "→ продолжить"

_VOICE_OPTIONS = (
    ("local", "Локальный сервис (рекомендуется)", "распознаёт офлайн, на вашем компьютере"),
    ("cloud", "Свой endpoint", "любой OpenAI-совместимый STT-сервис"),
    ("skip", "Без голосового ввода", ""),
)
# Место на диске: пока сервиса нет — это будущая цена установки, после неё — факт.
# 0.9 ГБ — замер штатной установки: окружение сервиса 390 МБ + модель 486 МБ = 876 МБ.
# Считаем десятичными (как трафик и как продают диски); в двоичных это 0.82 ГиБ, но
# на диске с учётом кластеров выходит даже больше — округляем вверх, не вниз.
_VOICE_LOCAL_DISK = {False: "скачает ~0.9 ГБ", True: "занимает ~0.9 ГБ на диске"}
_VOICE_SUMMARY_LABELS = {"local": "локальный сервис", "cloud": "свой endpoint", "skip": "нет"}
# Цена локального сервиса в ресурсах (замер на faster-whisper-small): модель висит в
# памяти постоянно — так задано WHISPER__TTL=-1, иначе каждая фраза начиналась бы с
# её загрузки. 4 потока — дефолт cpu_threads движка, а не свойство конкретной машины.
_VOICE_LOCAL_COST = "~1 ГБ RAM · CPU до 4 потоков"
# Группы курсора шага голоса (спека §4.1): стрелки двигают курсор ВНУТРИ группы,
# Tab переносит его МЕЖДУ группами.
_VOICE_GROUP_VARIANTS = "variants"
_VOICE_GROUP_FIELDS = "fields"
# Обещание Tab в футере: честный глагол зависит от группы под курсором. Тот же словарь
# в `/settings` (`settings._TAB_TO_*`) — одна клавиша не может называться на двух
# экранах по-разному; сторож — test_footer_contract.
_TAB_TO_FIELDS = "Tab — вниз к настройкам"
_TAB_TO_VARIANTS = "Tab — вверх к выбору"
# Раскрытый список забирает клавиши себе — футер на это время становится его: обещать
# «Enter — далее», когда Enter подставляет строку списка, значит врать (спека §6.4).
# Списков в мастере три: пресет-лист адреса «Подключения», пресет-лист адреса «Своего
# endpoint» и список моделей самого endpoint'а. Когда какой раскрывается — канон
# 2026-08-17, он описан у `_FOOTER_PRESET_LIST` ниже.
_FOOTER_MODEL_LIST = ("↑↓ — выбор модели  ·  Enter — подставить  ·  Esc — свернуть"
                      "  ·  PgUp — назад  ·  PgDn — далее")
# Фильтр не оставил строк (человек печатает свой id): стрелки список съедает, никуда
# не двигая, а `pick_in` возвращает False — подставлять нечего. Говорим теми же словами,
# что хинт под рамкой (`_MODEL_HINT_NO_MATCH`), иначе одна клавиша на одном экране
# зовётся двумя разными глаголами.
_FOOTER_MODEL_LIST_NO_MATCH = ("Enter — сохранить как есть  ·  Esc — свернуть"
                               "  ·  PgUp — назад  ·  PgDn — далее")
# Пресет-лист адреса «Своего endpoint» на шаге «Голосовой ввод» (спека 2026-08-05 §3) —
# то же поле группы «поля», тот же приоритет списка над лентой, но глагол — «подставить»,
# БЕЗ «и сохранить»: ЧУЖОЙ пресет тем же Enter'ом стирает поле «Модель» (§4.1 плана), и
# подстановка не доводит форму до конца шага. Канон — тот же текст, что у пресет-листа
# шага «Подключение» (`_FOOTER_PRESET_LIST` ниже), который по той же причине не обещает
# «далее».
_FOOTER_URL_LIST = ("↑↓ — выбор адреса  ·  Enter — подставить  ·  Esc — свернуть"
                    "  ·  PgUp — назад  ·  PgDn — далее")
# Свой адрес выбил из списка все строки — тот же случай, что у модели чуть выше.
_FOOTER_URL_LIST_NO_MATCH = ("Enter — оставить свой адрес  ·  Esc — свернуть"
                             "  ·  PgUp — назад  ·  PgDn — далее")
# Пресет-лист адреса на шаге «Подключение» раскрывается САМ, пока поле пусто (канон
# 2026-08-17) — то есть на первом запуске человек застаёт это состояние по умолчанию,
# а не ищет его: Enter подставляет провайдера и возвращается, Esc сворачивает список,
# а не выходит из мастера. Как только адрес задан (вернулись на шаг назад, подставили
# пресет), список свёрнут: стрелки водят по форме, а вызвать его можно `Alt+↓` —
# он назван строкой под полем. Ctrl+Space до списка не относится и работает как везде
# на шаге — проверяет связь.
_FOOTER_PRESET_LIST = ("↑↓ — выбор провайдера  ·  Enter — подставить  ·  Esc — свернуть"
                       "  ·  Ctrl+Space — проверить связь  ·  PgDn — далее")
# Список раскрыт, но подсветки нет: в редакции канона 2026-08-18 печать без совпадений
# СВОРАЧИВАЕТ список (`connection.PRESET_NO_MATCH_CLOSED`), так что состояние достижимо
# только форсированным `Alt+↓` поверх своего адреса. Подставлять нечего (`pick()` вернёт
# None), Enter лишь сворачивает список, оставляя набранное, — а вот ↑↓ работают: первая
# же стрелка подсвечивает строку. Раз работают — обязаны быть названы (тот же сегмент
# стоит в парном футере /settings).
_FOOTER_PRESET_LIST_NO_MATCH = ("↑↓  ·  Enter — оставить свой адрес  ·  Esc — свернуть"
                                "  ·  Ctrl+Space — проверить связь  ·  PgDn — далее")
# Поля «Своего endpoint», по которым ходит курсор группы полей. Мастер спрашивает три
# (язык — только в /settings, спека §10); тем же набором монтируется и сам редактор,
# чтобы лента и поля не разъехались.
_VOICE_FIELD_KEYS = ("url", "token", "model")


class OnboardingScreen(Screen):
    # Модальный экран: конфига ещё нет, глобальная навигация (F1/F4/…) поверх мастера
    # показывала бы справку с неработающими клавишами и уведомления по незаписанному
    # конфигу. Гейт — TuiApp.check_action.
    BLOCKS_GLOBAL_NAV = True

    # Хвост плашки о перекрывающем профиле: экран мастера — единственная поверхность,
    # где «Настройки · Профили» существуют, поэтому только он вправе звать туда.
    _NOTE_SETTINGS_TAIL = " · изменить в Настройки · Профили"

    # priority=True — перехватывает PgUp/PgDn раньше фокусированного Input (иначе поля
    # #conn-url/#conn-token/#onb-model/#voice-* не отдали бы клавишу навигации).
    BINDINGS = [
        Binding("pageup", "go_back", "назад", show=False, priority=True),
        Binding("pagedown", "go_next", "далее", show=False, priority=True),
    ]

    def __init__(self, config, config_path, *, review_all, on_commit,
                 fetch=None, mcp=None, voice=None, probe=None,
                 deactivate_profile=False, degraded_note=None) -> None:
        super().__init__()
        self._config = config
        self._config_path = config_path
        # Лестница загрузки приземлилась на ступень «без профиля»: конфиг собрался
        # только потому, что профиль сняли на этот запуск, и финал обязан снять
        # указатель совсем — иначе гейт записи отвергнет собранное мастером
        # (стек следующего старта снова поднимет сломанный профиль). Признак —
        # один флаг, а не копия `RepairLoad`: экрану от лестницы нужен ровно этот
        # ответ.
        self._deactivate_profile = deactivate_profile
        # Текст деградации загрузки (`RepairLoad.message`, None — деградации не было).
        # Показывается на самом экране, а не только рантайм-уведомлением: под мастером
        # глобальная навигация заблокирована намеренно (`BLOCKS_GLOBAL_NAV`), и до его
        # финала уведомление достать нечем — человек проходил все шаги, не понимая,
        # почему поля выглядят иначе, чем его файл (живой смок).
        self._degraded_note = degraded_note
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
        if voice is None:
            import tui.services.voice_setup as voice_mod
            self._voice_setup = voice_mod
        else:
            self._voice_setup = voice
        # Вердикт шага «Подключение» — только по chat/completions: models-цепочка
        # у публичного /models (OpenRouter) отвечает без токена и сказала бы
        # «доступен» про маршрут, который токен как раз требует.
        from tui.services.settings_probe import probe_auth as _default_probe
        self._probe_fn = probe or _default_probe
        self._steps = _BASE_STEPS
        self._voice_choice = ""            # "" | "local" | "cloud" | "skip"
        self._voice_group = _VOICE_GROUP_VARIANTS   # группа под курсором (спека §4.1)
        self._voice_field_idx = 0          # позиция в группе полей — своя у каждой группы
        self._voice_cloud: dict = {}       # url/token/model (выбор cloud)
        # Ctrl+U на «Токене» голосового шага: пустое поле теперь значит «стереть
        # сохранённый», а не «оставить как было» (флаг переживает пересборку полей).
        self._voice_token_cleared: bool = False
        self._voice_prefilled = False      # status_cheap на шаге голоса — не чаще раза
        self._step = 0
        self._levels_stage = "ask"     # "ask" (две строки) | "light" | "max" (под-форма)
        self._levels_idx = 0
        self._values: dict = {}
        self._idx: dict = {}
        self._mcp_selected: set = set()
        self._mcp_installed: set = set()   # уже подключённые (wired) — отмечаем, не предлагаем
        self._probe_gen = 0   # generation авто-пробы шага «Подключение»: протухший результат отбрасывается
        self._last_probed_snapshot: tuple | None = None   # ConnectionEditor.probe_snapshot() последней пробы

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
                    # Обе плашки — отдельными строками одного вида, и деградация
                    # стоит первой: она объясняет СОСТОЯНИЕ («вот почему поля не
                    # такие, как в вашем файле»), а вердикт о слое — БУДУЩЕЕ записи
                    # («вот что перекроет ваш выбор»); второе — следствие первого,
                    # когда финал снимает профиль. Общим блоком с заголовком их не
                    # сводим: заголовков у мастера нет нигде, а склеенные в один
                    # абзац два длинных предложения читаются как одно.
                    if self._degraded_note:
                        yield Static(self._degraded_note, id="onb-degraded-warn",
                                     classes="onb-warn", markup=False)
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
        # Профиль, который финал деактивирует, перекрыть выбор уже не сможет — и
        # плашка о нём противоречила бы предупреждению о деактивации строкой ниже.
        note = onboarding.overriding_layer_note(
            config, self._config_path, ignore_profile=self._deactivate_profile)
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

    def _model_step_current(self) -> str:
        """Что шаг «Модель» называет текущим (`onboarding.model_step_display`, M12).
        Знание общее с плоским мастером и живёт в ядре: вторая его редакция уже
        расходилась с первой — тот же шаг здесь показывал модель АКТИВНОГО уровня,
        хотя задаёт слот `default`.

        `model_step_display`, а не голый `model_step_current` (ревью #14): при
        незаданном `default` с заданным другим уровнем вторая честно отвечает
        пусто (правило «не выдумывать назначенное»), а строка «сейчас» самого
        шага (`_render_body`) уже показывает модель фоллбэка — единственный
        читатель этого метода, `_shown_values`, обязан называть ТО ЖЕ САМОЕ, иначе
        сводка и лента подтверждения одного экрана говорят «—» там, где шаг рядом
        уже назвал модель."""
        return onboarding.model_step_display(self._config or {})[0]

    def _assigned_level_model(self, level: str) -> str:
        """Модель, явно назначенная уровню (`onboarding.assigned_level_model`)."""
        return onboarding.assigned_level_model(self._config or {}, level)

    def _level_value(self, level: str) -> str:
        """Что показать против уровня: набранное в этом прогоне мастера, иначе —
        назначенное в конфиге. Один источник на список уровней и на его под-форму:
        разойдясь, они показали бы одному человеку два разных ответа на один вопрос."""
        return (self._values.get(onboarding.MODEL_SLOT_KEYS[level])
                or self._assigned_level_model(level))

    def _shown_values(self) -> dict:
        """Чем витрины мастера (лента-сводка и подтверждение) заполняют то, чего
        человек сам не набрал, — резолвнутым конфигом запуска. При активном профиле
        это значения его слоя: витрина говорит о том, чем агент реально будет
        работать, а плашка рядом объясняет, откуда они (спека §5).

        Модель — та же, о которой говорит шаг «Модель» (слот `default`), а не резолв
        активного уровня: обе витрины называют её именем СЛОТА («модель default:»
        на подтверждении, «модель:» в ленте рядом с ним), и при `level: max` на одном
        экране стояли «модель default: <модель max>» и «модель max: <она же>». Слой
        профиля от этого никуда не девается — читается тот же `self._config`."""
        return {"completions_url": self._config.get("completions_url"),
                "model": self._model_step_current(),
                "Authorization": (self._config.get("chat_completion_headers") or {}
                                  ).get("Authorization")}

    def _summary_text(self) -> str:
        from tui.services.onboarding_summary import summary_line
        shown = self._shown_values()
        # Шаблонные заглушки конфига (…_SENTINEL) — это «ещё не задано», а не значение:
        # показываем прочерком, как редактор адреса трактует URL_SENTINEL пустым полем.
        url = onboarding.configured_endpoint(
            self._values.get("completions_url") or shown.get("completions_url"))
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
            voice=_VOICE_SUMMARY_LABELS.get(self._voice_choice, ""),
        )

    def _footer_text(self) -> str:
        step = self._step_id()
        if step == STEP_CONFIRM:
            return "Enter — подтвердить и завершить настройку  ·  PgUp — назад  ·  Esc — выход"
        ctrl = self._voice_focused_list()
        if ctrl is not None and ctrl.is_open:
            # Раскрытым может быть список URL или список модели (оба живут в группе
            # «поля» шага голоса, оба забирают клавиши себе) — глагол Enter у них
            # разный (см. константы), и футер обязан назвать ИМЕННО тот список.
            if self._voice_focused_field_key() == "url":
                return (_FOOTER_URL_LIST if ctrl.has_selection
                        else _FOOTER_URL_LIST_NO_MATCH)
            return (_FOOTER_MODEL_LIST if ctrl.has_selection
                    else _FOOTER_MODEL_LIST_NO_MATCH)
        if step == STEP_CONNECTION and self._presets_open():
            return (_FOOTER_PRESET_LIST if self._presets_have_selection()
                    else _FOOTER_PRESET_LIST_NO_MATCH)
        # Курсор в полях меняет не только Tab: Space там печатает пробел (Input съедает
        # клавишу раньше шага) и пометки не ставит — обещать им выбор нельзя (§6.4).
        in_fields = (self._step_id() == STEP_VOICE
                     and self._voice_cursor_group() == _VOICE_GROUP_FIELDS)
        parts = ["↑↓ навигация", "Enter — далее", "Esc — выход"]
        # Enter ведёт далее не везде: на помеченных шагах он ставит пометку под
        # курсором. «Далее» в эти моменты — PgDn.
        enter_chooses = (step in _MARK_STEPS and not self._step_marked()
                         # …кроме строки-действия списка уровней: там Enter и есть «далее».
                         and not (step == STEP_LEVELS and self._levels_stage == "ask"
                                  and self._levels_on_continue()))
        if enter_chooses:
            # На шаге голоса ту же пометку ставит Space, и назвать его обязательно:
            # поля endpoint показаны уже под курсором, но клавиши до выбора принадлежат
            # шагу. Клавиши названы вместе — отдельным пунктом строка уходит за 100 колонок.
            choose = ("Space/Enter — выбрать"
                      if step == STEP_VOICE and not in_fields else "Enter — выбрать")
            parts[1:2] = [choose, "PgDn — далее"]
        elif step == STEP_VOICE and not in_fields:
            # Выбор уже стоит: Enter ведёт дальше, а Space остаётся тем, чем переставляют
            # его на другой вариант.
            parts.insert(1, "Space — выбрать")
        # Развод §2 спеки 2026-08-06: раскрытие/перечитывание списка ушло на Alt+↓,
        # Ctrl+Space осталась только там, где есть проба (карта §2.5) — на шаге
        # «Подключение» она проверяет связь, на «Модели» и «Голосовом вводе» пробы нет
        # вовсе и клавиша в футере не обещается (§5.1 спеки). На шаге «Подключение»
        # список свёрнут ровно в этой ветке (раскрытый уходит футером выше) — тогда
        # футер обязан назвать способ его вернуть, иначе `Esc` увёл бы список без пути
        # назад. На шаге голоса Alt+↓ называет подсказка под рамкой самого списка, а из
        # футера она выпадает: там уже сказано, где клавиша и работает.
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
        if tab := self._voice_tab_hint():
            parts.insert(1, tab)      # клавиша курсора — рядом с ↑↓, а не в хвосте
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

    def _voice_tab_hint(self) -> str:
        """Обещание Tab для футера или пустая строка, если переходить некуда (§6.1):
        на других шагах и на варианте без полей клавиша перехвачена, но молчит (§4.2.6),
        и называть её значило бы врать."""
        if self._step_id() != STEP_VOICE or len(self._voice_groups()) < 2:
            return ""
        return (_TAB_TO_VARIANTS if self._voice_cursor_group() == _VOICE_GROUP_FIELDS
                else _TAB_TO_FIELDS)

    def _step_marked(self) -> bool:
        """Помечена ли ● строка под курсором текущего шага. От этого зависит роль Enter
        (выбрать vs далее) и текст футера; шаги без пометки (Подключение, MCP) — False.

        Список уровней пометки не знает вовсе: Enter там всегда открывает под-форму
        уровня, «далее» со шага уводит PgDn."""
        from textual.css.query import NoMatches
        step = self._step_id()
        if step == STEP_LEVELS and self._levels_stage == "ask":
            return False
        if step == STEP_VOICE:
            cur = self._idx.get(STEP_VOICE, 0)
            return self._voice_choice == _VOICE_OPTIONS[cur][0]
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
                cur = onboarding.configured_endpoint(raw.get("completions_url"))
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
            body.mount(Static(_of.MODEL_STEP_HINT, classes="onb-hint onb-dyn set-spaced",
                              markup=False))
            endpoint, auth = self._endpoint_auth()
            # Строка «сейчас» шага — правило ядра, общее с плоским мастером
            # (`onboarding.model_step_display`, M12): при незаданном default с
            # заданным другим уровнем cfg_model — модель фоллбэка, и предвыбор
            # редактора ведётся к ней (проход насквозь = «оставить как есть»),
            # а не к первой модели сервера; подпись об источнике — строкой-хинтом
            # (в строку списка её не вставить — слово «текущая» рисует редактор).
            cfg_model, fallback_note = onboarding.model_step_display(self._config or {})
            if fallback_note:
                body.mount(Static(f"сейчас: {cfg_model}  {fallback_note}",
                                  classes="onb-hint onb-dyn", markup=False))
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
        elif step == STEP_VOICE:
            self._prefill_voice_from_config()
            self._idx.setdefault(STEP_VOICE, 0)
            # Тело шага пересобрано — курсор в первой группе, и позиция полей вместе с
            # ним: поля смонтированы заново и фокуса не держат, а курсор, оставшийся в
            # них с прошлого раза (уход со шага PgUp/PgDn), обещал бы движение по полям
            # от пустого места. Позиция без группы — половина сброса: первый же Tab
            # встал бы на «Модель», а вход на это поле сразу идёт на сервер за списком.
            self._voice_group = _VOICE_GROUP_VARIANTS
            self._voice_field_idx = 0
            installed = self._voice_installed()
            col = self._voice_desc_col(installed)
            for i, (key, title, desc) in enumerate(_VOICE_OPTIONS):
                owner = i == self._idx[STEP_VOICE]
                # Вторая строка описания: у локального — во что он обходится (всегда,
                # иначе список дёргается на строку при каждом движении курсора). Про
                # клавиши здесь не говорим: подсказки про них живут в футере (спека §4.3).
                second = None
                if key == "local":
                    second = self._voice_cost_text()
                # Свой класс-маркер строкам режима обязателен: `.onb-option` носят и
                # строки раскрытого списка моделей (они внутри полей этого же шага), и
                # запрос по нему возвращал бы их вперемешку со строками режима.
                cls = "onb-option onb-dyn onb-mcp-row onb-voice-row"
                if second is None:
                    # Воздух в ДВЕ строки: вариант без второй строки описания добирает
                    # ею недостающую высоту, и блоки вариантов встают поровну (смок
                    # владельца 2026-08-05).
                    cls += " set-spaced-2"
                # Со второй строкой воздух уходит ПОД неё, иначе она отрывается от своей
                # опции и прилипает к следующей.
                if owner and self._voice_highlight_variants():
                    cls += " selected"
                body.mount(Static(
                    self._voice_row_text(owner, key, title, desc,
                                         installed=installed), classes=cls))
                if second is not None:
                    body.mount(self._voice_second_line(second, col))
            if self._voice_cursor_is_cloud():
                self._mount_voice_fields(body)
        elif step == STEP_CONFIRM:
            from onboarding import mask_authorization
            shown = self._shown_values()
            addr = onboarding.configured_endpoint(
                self._values.get("completions_url") or shown.get("completions_url")) or "—"
            model = self._values.get("model") or shown.get("model") or "—"
            # Плейсхолдеры шаблона — не значения (тот же канон, что в строке-сводке
            # выше и в витринах базы): нетронутое поле в _values не попадает, а
            # `agent.model` мастер с волны B не пишет вовсе — у вернувшегося в мастер
            # человека оба плейсхолдера так и лежат в конфиге.
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
            # Витрина показывает состояние КОНФИГА, а не историю ответов этого прогона:
            # уровень, заданный раньше и не тронутый сейчас, обязан остаться видимым —
            # иначе одна и та же модель то есть в сводке, то пропадает в зависимости от
            # того, заходил ли человек на её под-форму (`_level_value`, см. выше).
            for level in _LEVEL_ROWS:
                value = self._level_value(level)
                rows.append((f"модель {level}:", value or _of.LEVEL_UNSET))
            rows += [("токен:", tok), ("MCP:", mcp_val)]
            if self._voice_choice == "cloud":
                host = (self._voice_cloud.get("url", "").split("/")[2]
                        if "://" in self._voice_cloud.get("url", "") else "—")
                voice_val = f"endpoint {host}"
            elif self._voice_choice == "local":
                from voice_runtime.state import current_model_short
                voice_val = f"локальный сервис · {current_model_short()}"
            else:
                voice_val = "нет"
            rows.append(("голосовой ввод:", voice_val))
            summary = Text()
            label_width = max(len(label) for label, _ in rows) + 1  # колонка под самую длинную метку
            for n, (label, val) in enumerate(rows):
                if n:
                    summary.append("\n")            # воздух между строками сводки
                # Галочка — про ЗАПОЛНЕННОЕ: «✓ адрес: —» читается «готово» там, где
                # ничего нет. Глиф гасим, а не убираем — колонка меток не съезжает.
                summary.append("  " if val in ("—", _of.LEVEL_UNSET) else "✓ ", style=OK)
                summary.append(f"{label:{label_width}}", style=DIM)
                summary.append(f"{val}\n", style=INK)
            if self._deactivate_profile:
                # Enter отсюда не только пишет настройки, но и снимает указатель
                # профиля — предупреждение стоит ДО нажатия, а не в отчёте после:
                # человек не должен узнавать о потере своей настройки постфактум.
                # Текст — общий с плоским мастером (`_of`), цветом предупреждения:
                # это единственная строка финала, говорящая об отнятом.
                summary.append("\n")
                summary.append(_of.PROFILE_DEACTIVATION_WARNING + "\n", style=WARN)
            notice_parts = []
            if self._mcp_selected:
                notice_parts.append(f"MCP ({mcp_val})")
            if self._voice_choice == "local" and not self._voice_installed():
                notice_parts.append("локальный голосовой сервис (~0.9 ГБ)")
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
        уровней): у них один и тот же эндпоинт, переспрашивать его нечего.

        Заглушка адреса — не адрес (`configured_endpoint`, то же правило, что у
        витрин шага): по ней не стучимся, а ведём себя как на чистой установке —
        адреса нет, списка нет, остаётся ручной ввод."""
        endpoint = onboarding.configured_endpoint(
            self._values.get("completions_url") or (self._config or {}).get("completions_url"))
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
            body.mount(Static(_of.LEVELS_QUESTION, classes="onb-hint onb-dyn set-spaced",
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
        body.mount(Static(_of.LEVEL_FORM_LABEL[level], classes="onb-hint onb-dyn", markup=False))
        endpoint, auth = self._endpoint_auth()
        current = self._level_value(level)
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
        value = self._level_value(level)
        width = max(len(slot_head(name)) for name in _LEVEL_ROWS) + 2
        t = Text()
        t.append("▸ " if is_cursor else "  ", style=ACCENT)
        head = slot_head_text(level)
        head.pad_right(max(0, width - len(head.plain)))
        t.append(head)
        t.append(value or _of.LEVEL_UNSET, style=DIM if value else DEEP_DIM)
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

    def _append_desc(self, t: Text, desc: str, col: int | None = None) -> None:
        """Дописать приглушённое описание с выравниванием на колонку (приём — как
        McpSectionEditor._row_text в /settings).

        `col` — своя колонка шага, когда левые части там длиннее общей: строки
        голосовых режимов считают её от пометки установки (`_voice_desc_col`).
        Без аргумента работает общая `_DESC_COL` — второго дома у паддинга нет."""
        if not desc:
            return
        pad = (col or self._DESC_COL) - t.cell_len
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

    def _voice_installed(self) -> bool:
        try:
            from voice_runtime.state import load_state
            return load_state() is not None
        except Exception:
            return False

    def _voice_row_left(self, is_cursor: bool, key: str, title: str, *,
                        installed: bool) -> Text:
        """Левая часть строки-опции: курсор, кружок, название и статус."""
        t = Text()
        t.append("▸ " if is_cursor else "  ", style=ACCENT)
        t.append("(")
        t.append("●" if self._voice_choice == key else " ", style=INK)
        t.append(f") {title}")
        if key == "local" and installed:
            t.append(" · установлен", style=DEEP_DIM)
        return t

    def _voice_desc_col(self, installed: bool) -> int:
        """Колонка описаний шага — по самой длинной левой части, а не константой:
        статус «установлен» появляется только у локальной строки и только когда сервис
        стоит. С фиксированной колонкой описания повисали бы в пустоте, пока его нет."""
        widest = max(self._voice_row_left(False, key, title, installed=installed).cell_len
                     for key, title, _ in _VOICE_OPTIONS)
        return widest + 2

    def _voice_row_text(self, is_cursor: bool, key: str, title: str, desc: str,
                        *, installed: bool) -> Text:
        """Строка опции голосового шага — тот же язык, что _mcp_row_text."""
        t = self._voice_row_left(is_cursor, key, title, installed=installed)
        if key == "local":
            desc = f"{desc}; {_VOICE_LOCAL_DISK[installed]}"
        # Модель — во вторую строку, к остальной цене сервиса: в первой она уводила
        # строку за 128 колонок, и на узком окне описание обрывалось многоточием.
        self._append_desc(t, desc, self._voice_desc_col(installed))
        return t

    def _voice_second_line(self, text: str, col: int) -> Static:
        """Вторая строка описания опции.

        Колонка задаётся отступом виджета, а не пробелами в тексте: на узком окне
        строка переносится, и пробелы удержали бы только первую строку — хвост уезжал
        бы к левому краю. +1 — собственный отступ строки-опции, чтобы описания встали
        ровно друг под друга."""
        w = Static(text, classes="onb-hint set-spaced onb-dyn")
        w.styles.padding = (0, 0, 0, col + 1)
        return w

    def _voice_cost_text(self) -> str:
        """Чем локальный сервис занят и какой моделью."""
        from voice_runtime.state import current_model_short
        return f"{current_model_short()} · {_VOICE_LOCAL_COST}"

    def _prefill_voice_from_config(self) -> None:
        """Шаг голоса открывается на ДЕЙСТВУЮЩЕМ режиме, а не на первом варианте.

        Мастер проходят и на настроенном конфиге (`--init-config`). Без префилла
        пролистывание шага читалось как выбор «локальный сервис»: _voice_advance
        берёт вариант под курсором, а тот по умолчанию нулевой. Дальше _commit
        ставит сервис и прописывает managed-слой, который перекрывает юзерский
        voice.url — облачный провайдер молча переставал действовать.

        Гейт — «прочитали снимок один раз за экран», а не «выбор ещё не сделан»:
        тело шага монтируется заново на каждое движение курсора (смена опции меняет
        _voice_body_shape), и при mode == "off" (или недоступном состоянии) выбор
        так и остаётся пустым — с гейтом по _voice_choice снимок спрашивался
        бы на каждую стрелку. Снимок — дешёвый (status_cheap): нужен только mode,
        а полный ходил бы в порт сервиса прямо с UI-потока.
        """
        if self._voice_prefilled:
            return
        self._voice_prefilled = True
        try:
            snap = self._voice_setup.status_cheap(self._config or {})
        except Exception:
            return                      # состояние недоступно — ведём себя как раньше
        mode = getattr(snap, "mode", "off")
        if mode not in ("local", "cloud"):
            return                      # ничего не настроено — первичный сценарий, дефолт
        self._voice_choice = mode
        for i, (key, _title, _desc) in enumerate(_VOICE_OPTIONS):
            if key == mode:
                self._idx[STEP_VOICE] = i
                break
        if mode == "cloud" and not self._voice_cloud:
            voice_cfg = self._voice_raw_cloud()
            self._voice_cloud = {
                "url": str(voice_cfg.get("url") or ""),
                # Токен — секрет: в поле не кладём, показывает маской placeholder
                # (канон «Подключения»: пустое поле = «оставить сохранённый»).
                "token": "",
                "model": str(voice_cfg.get("model") or ""),
                "language": str(voice_cfg.get("language") or "ru"),
            }

    def _voice_raw_cloud(self) -> dict:
        """voice-секция СЫРОГО config.yml (канон /settings, `SettingsScreen._voice_raw_cloud`):
        self._config слит с managed-слоем и с уже раскрытыми ${ENV:...} — для полей
        «Своего endpoint» не годится. Иначе managed-заглушка локальной батарейки
        (`voice.token: "local"`, см. voice_runtime.wire) читалась бы как сохранённый
        облачный секрет, а ${ENV:...}-ссылка на коммите запекалась бы в файл литералом."""
        if not self._config_path:
            return {}
        import config_raw
        return config_raw.read_document(self._config_path).get("voice") or {}

    def _raw_conn(self) -> dict:
        """Сырые (нерезолвнутые) значения полей шага «Подключение» ИЗ ФАЙЛА — тот же
        приём, что `SettingsScreen._read_raw` / `_voice_raw_cloud` выше: self._config
        уже резолвнут (${ENV:...}/${FILE:...} подставлены), и показать или записать
        его значение обратно в файл значит подменить ссылку на переменную её значением —
        секрет открытым текстом в файле, который коммитят."""
        return self._raw_values("completions_url", "Authorization", "proxy")

    def _raw_values(self, *keys: str) -> dict:
        """{ключ-якорь: значение из базового файла как записано}. Файла нет — пусто."""
        if not self._config_path:
            return {}
        import config_raw
        from onboarding import FIELD_ANCHOR_PATHS
        values = config_raw.read_values(self._config_path, (FIELD_ANCHOR_PATHS[k] for k in keys))
        return {k: values[FIELD_ANCHOR_PATHS[k]] for k in keys}

    def _voice_verify_ssl(self):
        """voice.verify_ssl ДЕЙСТВУЮЩЕГО конфига (тот же приём, что
        `SettingsScreen._voice_verify_ssl`) или True — проверять сертификат по умолчанию."""
        voice = self._config.get("voice") if isinstance(self._config, dict) else None
        if not isinstance(voice, dict) or "verify_ssl" not in voice:
            return True
        return voice["verify_ssl"]

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

    def _mount_voice_fields(self, body) -> None:
        """Поля «Свой endpoint» — канон-рамки редактора (те же классы, что у шага
        «Подключение»); фокус в них ставит курсор ленты, а не сам редактор.

        Список моделей берётся с самого endpoint (тем же фасадом, что в /settings):
        адрес и токен к этому моменту уже набраны полями выше, а пришедший список —
        заодно и подтверждение, что связка рабочая. `url_presets` — тот же список
        пресетов СТТ-адресов (спека 2026-08-05 §3), что у /settings: поле «URL»
        получает свой раскрывающийся список ровно тем же путём, что «Модель»."""
        from tui.editors.voice_endpoint import VoiceEndpointEditor
        from onboarding import display_secret
        from config_modules.voice_endpoint_presets import voice_endpoint_presets_from_config
        # Сохранённый токен не подставляем (секрет), но и молчать нельзя — маска в
        # placeholder, пустое поле значит «оставить сохранённый» (канон /settings).
        raw_token = self._voice_raw_cloud().get("token")
        placeholders = {"token": display_secret(raw_token)} if raw_token else None
        # Фокуса на монтировании нет: поля — вторая ГРУППА шага (спека §4.1), и курсор
        # входит в неё только по Tab. Забрать фокус сразу значило бы вернуть рассинхрон
        # «фокус в поле, курсор в вариантах» — печать и Space доставались бы полю, а
        # стрелки при этом двигали бы строки режима.
        body.mount(VoiceEndpointEditor(values=self._voice_cloud,
                                       fields=_VOICE_FIELD_KEYS,
                                       autofocus=False,
                                       fetch=self._voice_setup.list_models,
                                       # Пустое поле токена в списке моделей значит
                                       # «сохранённый» (тот же канон, что при коммите) —
                                       # без него запрос на рабочем облаке 401-ит.
                                       saved_token=str(raw_token or ""),
                                       placeholders=placeholders,
                                       # TLS/прокси запроса за списком — тот же канон,
                                       # что /settings (`SettingsScreen._mount_voice_section`):
                                       # без них список уходит с verify=True мимо
                                       # backend.proxy, и в корп-сети (self-signed TLS,
                                       # обязательный прокси) рабочий endpoint выглядит
                                       # как «не удалось получить список».
                                       verify_ssl=self._voice_verify_ssl(),
                                       config_proxy=self._effective_proxy() or None,
                                       # Пресеты СТТ-адресов (спека 2026-08-05 §3) — тот же
                                       # источник, что у /settings: дефолты (OpenAI/Groq/
                                       # Mistral) или кастомизация из config.yml.
                                       url_presets=voice_endpoint_presets_from_config(
                                           self._config or {})))

    def _voice_endpoint_editor(self):
        """Редактор полей «Своего endpoint» или None (не тот шаг / поля свёрнуты).

        ПОСЛЕДНИЙ матч, а не первый: `remove()` в Textual отложенный, и сразу после
        перерисовки шага старый редактор ещё висит в дереве. `.first()` отдавал бы
        именно его — обречённый, со значениями прошлого рендера. Канон файла настроек
        (`settings._voice_editor`), здесь он был нарушён в обе стороны: тут брали
        первый, а на сабмите — `query_one`, который на двух матчах просто падал."""
        if self._step_id() != STEP_VOICE:
            return None
        from tui.editors.voice_endpoint import VoiceEndpointEditor
        eds = list(self.query(VoiceEndpointEditor))
        return eds[-1] if eds else None

    def _voice_model_list(self):
        """Контроллер списка моделей шага или None (не тот шаг / полей нет)."""
        ed = self._voice_endpoint_editor()
        return ed.model_list if ed is not None else None

    def _voice_clear_token(self) -> bool:
        """Ctrl+U в полях голосового шага: стереть сохранённый токен. False — клавиша
        не наша (фокус не на «Токене» либо стирать нечего), и её отдают полю ввода."""
        ed = self._voice_endpoint_editor()
        if ed is None:
            return False
        return ed.clear_token(self._voice_focused_field_key())

    def _voice_focused_field_key(self) -> str | None:
        """Ключ поля шага голоса под ФОКУСОМ (url/token/model) или None — фокус не на
        нём. У «Токена» списка нет (`list_for` отдаёт по нему None) — сверка тут только
        про то, ЧТО в фокусе, список из неё считает вызывающий сам.

        Раньше здесь был отдельный `_voice_focus_in_model` (сверял id ровно с полем
        модели): у «URL» теперь свой пресет-лист (спека 2026-08-05 §3), и раскрытый
        список обязан забирать клавиши себе так же, как у модели — сверка сузилась бы
        до одного поля и молчала бы про второе. `_key_of_input` — тот же приём, что
        внутри `VoiceEndpointEditor.on_input_changed`."""
        from tui.editors.voice_endpoint import _key_of_input
        return _key_of_input(getattr(self.focused, "id", None))

    def _voice_focused_list(self):
        """Контроллер списка поля под ФОКУСОМ (модель ИЛИ URL) шага или None — не тот
        шаг / полей нет / фокус не в списковом поле. Общая точка для `on_key`
        (Esc/стрелки/Enter раскрытого списка — Ctrl+Space он больше не забирает,
        спека 2026-08-06 §2.2) и футера — оба обязаны видеть один и тот же список."""
        ed = self._voice_endpoint_editor()
        return ed.list_for(self._voice_focused_field_key()) if ed is not None else None

    def on_descendant_focus(self, event) -> None:
        """Клик мышью по полю — это тоже перемещение курсора (спека §4.3, канон
        `SettingsScreen.on_descendant_focus`): подтягиваем к нему курсор группы, иначе
        следующая стрелка пойдёт из «прошлого» места, где он остался.

        Он же раскрывает список поля: фокус приходит и от `focus_field` ленты, и от
        мыши, и правило у обоих одно — список раскрыт там, где стоит курсор."""
        from tui.editors.voice_endpoint import _FIELDS
        keys = self._voice_field_keys()
        wid = getattr(event.widget, "id", None)
        key = next((k for k in keys if _FIELDS[k][1] == wid), None)
        if key is None:
            return                         # фокус не в полях шага — нам нечего сводить
        self._voice_group = _VOICE_GROUP_FIELDS
        self._voice_field_idx = keys.index(key)
        self._voice_refresh_rows()         # курсор в полях — подсветка строки гаснет
        self._sync_voice_field_lists()
        self._refresh_footer()             # мышь сменила группу — клавиши места с ней

    # --- группы курсора шага голоса (спека §4) ---
    def _voice_groups(self) -> tuple:
        """Группы шага: строки режима — всегда, поля «Своего endpoint» — пока они
        показаны (живут под своей опцией, см. `_voice_cursor_is_cloud`)."""
        if self._voice_cursor_is_cloud() and self._voice_endpoint_editor() is not None:
            return (_VOICE_GROUP_VARIANTS, _VOICE_GROUP_FIELDS)
        return (_VOICE_GROUP_VARIANTS,)

    def _voice_field_keys(self) -> tuple:
        """Поля, по которым ходит курсор группы полей (пусто — полей на экране нет)."""
        return _VOICE_FIELD_KEYS if self._voice_endpoint_editor() is not None else ()

    def _voice_cursor_group(self) -> str:
        """Где сейчас курсор: "variants" или "fields". Одно место правды — иначе
        состояние приходится выводить из побочных признаков (фокус, видимость полей), а
        они врут при клике мышью и при пустой группе. Хранимая группа сверяется с
        наличными: поля исчезают вместе с уходом курсора со «Своего endpoint»."""
        groups = self._voice_groups()
        return self._voice_group if self._voice_group in groups else _VOICE_GROUP_VARIANTS

    def _voice_field_position(self) -> int:
        """Позиция в группе полей, зажатая в её границы (спека §4.2.1): набор полей
        зависит от варианта под курсором, и запомненная позиция может оказаться за краем."""
        n = len(self._voice_field_keys())
        return min(max(self._voice_field_idx, 0), n - 1) if n else 0

    def _voice_cursor_position(self) -> int:
        """Позиция внутри текущей группы (у каждой она своя и помнится отдельно)."""
        if self._voice_cursor_group() == _VOICE_GROUP_FIELDS:
            return self._voice_field_position()
        return self._idx.get(STEP_VOICE, 0)

    def _voice_cursor_field(self) -> str | None:
        """Имя поля под курсором или None, если курсор в группе вариантов."""
        keys = self._voice_field_keys()
        if not keys or self._voice_cursor_group() != _VOICE_GROUP_FIELDS:
            return None
        return keys[self._voice_field_position()]

    def _move_group(self, delta: int) -> None:
        """Tab/Shift+Tab — курсор в соседнюю группу, по кругу (спека §4.1). Групп меньше
        двух — переходить некуда, и клавиша просто съедена (§4.2.6). Позиция группы, из
        которой уходим, остаётся при ней: возврат приводит курсор на прежнюю строку."""
        groups = self._voice_groups()
        if len(groups) < 2:
            return
        cur = groups.index(self._voice_cursor_group())
        self._voice_group = groups[(cur + delta) % len(groups)]
        if self._voice_group == _VOICE_GROUP_FIELDS:
            self._voice_field_idx = self._voice_field_position()   # запомненную — в границы
        self._voice_refresh_rows()     # курсор сменил группу — подсветка уходит за ним
        self._sync_voice_focus()

    def _move_voice_field(self, delta: int) -> None:
        """↑/↓ внутри группы полей, циклом. Раскрытый список забирает стрелки раньше —
        он выше по приоритету (спека §4.2.2), см. начало `on_key`."""
        keys = self._voice_field_keys()
        if not keys:
            return
        self._voice_field_idx = (self._voice_field_position() + delta) % len(keys)
        self._sync_voice_focus()

    def _sync_voice_field_lists(self) -> None:
        """Список поля раскрыт ровно тогда, когда курсор стоит на этом поле (канон
        `SettingsScreen._sync_voice_field_lists`): уход с поля его сворачивает, и
        соседний список раскрытым остаться не может. Свёрнутый Esc'ом сам не вернётся —
        курсор всё ещё на поле, второго входа не было (назад ведут печать и Alt+↓,
        спека 2026-08-06 §2, §4.1).

        Списков теперь три (`url_list` — пресеты адреса, спека 2026-08-05 §3), и все
        три обязаны попасть в перебор: пропуск `url_list` (как было до этой правки)
        оставлял его раскрытым, если курсор уходил с «URL» на «Модель» кликом мыши —
        `on_descendant_focus` меняет группу/поле, но список поля, где курсор БЫЛ,
        никто не закрывал."""
        ed = self._voice_endpoint_editor()
        if ed is None:
            return
        active = ed.list_for(self._voice_cursor_field())
        for ctrl in (ed.lang_list, ed.model_list, ed.url_list):
            if ctrl is not None and ctrl is not active:
                # Строку под полем `close()` приводит в согласие сам: с канона
                # 2026-08-17 она постоянна и называет способ («вот чем раскрыть»),
                # а не команду момента — снимать её с уходом курсора больше нечем.
                ctrl.close()
        if active is not None:
            active.open()

    def _voice_cursor_is_cloud(self) -> bool:
        """Курсор стоит на «Своём endpoint» — значит его поля показаны: поля живут под
        своей опцией, а не под пометкой (иначе они висят под чужой строкой и читаются
        как её)."""
        return _VOICE_OPTIONS[self._idx.get(STEP_VOICE, 0)][0] == "cloud"

    def _voice_highlight_variants(self) -> bool:
        """Красить ли подложкой строку режима. Курсор один (спека §4.1): ушёл в поля —
        подсветки нет ни у одной строки, иначе на экране два курсора сразу, строка и
        фокус-рамка поля. ▸ этому правилу не подчиняется — он маркер ВЛАДЕЛЬЦА
        показанных полей (канон `SettingsScreen._render_voice`)."""
        return self._voice_cursor_group() == _VOICE_GROUP_VARIANTS

    def _voice_refresh_rows(self) -> None:
        """Перерисовать строки опций (▸, подсветка, ●) БЕЗ пересборки тела шага: поля,
        фокус в них и раскрытый список остаются на месте."""
        installed = self._voice_installed()
        cur = self._idx.get(STEP_VOICE, 0)
        highlight = self._voice_highlight_variants()
        for i, w in enumerate(self.query(".onb-voice-row")):
            key, title, desc = _VOICE_OPTIONS[i]
            w.update(self._voice_row_text(i == cur, key, title, desc, installed=installed))
            (w.add_class if i == cur and highlight else w.remove_class)("selected")

    def _voice_body_shape(self) -> tuple:
        """Что показано под строками опций: поля endpoint и/или цена локального сервиса.
        Обе добавки принадлежат опции под курсором — сменилась она, меняется и тело."""
        cur = _VOICE_OPTIONS[self._idx.get(STEP_VOICE, 0)][0]
        return (cur == "cloud", cur == "local")

    def _move_voice(self, delta: int) -> None:
        n = len(_VOICE_OPTIONS)
        was_shape = self._voice_body_shape()
        if self._voice_cursor_is_cloud():
            self._capture_voice_fields()   # набранное — в черновик до того, как поля уйдут
        self._idx[STEP_VOICE] = (self._idx.get(STEP_VOICE, 0) + delta) % n
        if was_shape != self._voice_body_shape():
            self._render_step()            # состав тела сменился: добавка пришла/ушла
            return
        self._voice_refresh_rows()
        self._sync_voice_focus()
        self._refresh_footer()

    def _sync_voice_focus(self) -> None:
        """Связка «курсор ↔ фокус» (канон `SettingsScreen._sync_voice_focus`): курсор в
        группе полей — фокус в поле под ним (своего маркера у полей нет, индикатор — их
        фокус-рамка); курсор в вариантах — фокус снят, иначе Space и печать достаются
        полю, а не шагу."""
        ed = self._voice_endpoint_editor()
        key = self._voice_cursor_field()
        if ed is not None and key is not None:
            ed.focus_field(key)
        else:
            self.set_focus(None)
        self._sync_voice_field_lists()
        self._refresh_footer()   # группа и список меняют клавиши места — футер после них

    def _capture_voice_fields(self) -> None:
        """Снять набранное из полей endpoint без валидации: перевыбор другой опции
        сворачивает поля, и вернувшийся на endpoint человек должен увидеть свой URL."""
        from tui.editors.voice_endpoint import VoiceEndpointEditor
        for ed in self.query(VoiceEndpointEditor):
            self._voice_cloud = {**self._voice_cloud, **ed.values()}
            if ed.token_cleared:
                # Ctrl+U на «Токене»: пустота в черновике теперь ОСОЗНАННАЯ, и при
                # записи её нельзя подменить сохранённым секретом (см. _commit_config).
                self._voice_token_cleared = True

    def _voice_mark(self) -> None:
        """Пометить опцию под курсором (видимое ●), остаться на шаге (канон MCP-шага).
        Поля показывает курсор, а не пометка, поэтому тело шага здесь не пересобирается:
        набранное, фокус и раскрытый список из-за ● терять незачем.

        Курсор пометка не двигает (`Space` — тумблер строки, а не навигация), поэтому и
        фокус не сводит: сведение раскрыло бы список поля под курсором, и свёрнутый
        Esc'ом список возвращался бы сам."""
        if self._voice_cursor_is_cloud():
            self._capture_voice_fields()      # черновик догоняет набранное
        self._voice_choice = _VOICE_OPTIONS[self._idx.get(STEP_VOICE, 0)][0]
        self._voice_refresh_rows()
        self._refresh_chrome()

    def _voice_enter(self) -> None:
        """Enter на шаге голоса — двухролевой: опция под курсором не помечена → Enter
        её ПОМЕЧАЕТ (шаг тот же), помечена → Enter ведёт дальше. Для «Своего endpoint»
        это и есть два такта: первый ставит ● (поля показаны курсором и так, войти в них
        — Tab), второй валидирует их и идёт дальше."""
        cur = self._idx.get(STEP_VOICE, 0)
        if self._voice_choice == _VOICE_OPTIONS[cur][0]:
            self._voice_advance()
            return
        self._voice_mark()

    def _voice_advance(self) -> None:
        """«Далее» с шага голоса (Enter на помеченной опции или PgDn откуда угодно):
        выбор сделан — идём по нему, независимо от того, куда ушёл курсор; не сделан —
        берём вариант под курсором. Cloud без раскрытых полей сначала раскрывает их —
        в т.ч. когда выбор cloud поставил префилл, а курсор потом увели на другую
        опцию: поля свёрнуты и не смонтированы, брать из них нечего."""
        cur = self._idx.get(STEP_VOICE, 0)
        key = self._voice_choice or _VOICE_OPTIONS[cur][0]
        if key == "cloud":
            if self._voice_choice != "cloud":
                self._voice_mark()           # первый такт: пометить, поля уже под курсором
                return
            if not self._voice_cursor_is_cloud():
                # Выбор — cloud, но курсор сейчас на другой опции: поля endpoint не
                # смонтированы (живут под курсором, не под пометкой), query_one ниже
                # упал бы NoMatches. Тот же первый такт, что и пометка выше: вернуть
                # курсор на «Свой endpoint» и перерисовать шаг (поля показываются, а
                # войти в них — Tab), не продвигаясь дальше — новый выбор здесь не нужен.
                for i, (opt_key, _title, _desc) in enumerate(_VOICE_OPTIONS):
                    if opt_key == "cloud":
                        self._idx[STEP_VOICE] = i
                        break
                self._render_step()
                return
            editor = self._voice_endpoint_editor()
            if editor is None:
                return                          # редактора нет — сабмитить нечего
            ok, vals = editor.submit()
            if not ok:
                return                          # ошибка URL показана внутри редактора
            # Черновик хранит СЫРОЕ введённое (пусто — значит пусто): токен — секрет,
            # который поле намеренно не показывает (см. префилл), и подстановка
            # сохранённого не имеет права осесть здесь — иначе она материализуется в
            # поле при возврате на шаг. Фолбэк «пустое значит оставить сохранённый»
            # (канон «Подключения» — /settings._voice_cloud_save) — в _commit_config,
            # там же, где собирается запись.
            self._voice_cloud = {"url": vals["url"], "token": vals["token"],
                                 "model": vals["model"]}
            self._advance()
            return
        self._voice_choice = key
        self._advance()

    def _auto_probe_if_changed(self) -> None:
        """Авто-проба, если с прошлой изменилось хоть что-то из уходящего в запрос.

        Гейт против дребезга: ходьба по форме стрелками ничего не меняет, а без него
        пролистанная вверх-вниз форма давала реальный запрос на каждое нажатие.
        Живёт здесь, а не внутри `_auto_probe`: ручной `Ctrl+Space` обязан идти
        мимо снимка (§5.2), иначе вердикт «сервер недоступен» залипал бы навсегда."""
        ed = self._connection_editor()
        if ed is None:
            return
        snapshot = ed.probe_snapshot()
        if not snapshot[0] or snapshot == self._last_probed_snapshot:
            return          # адреса нет либо ровно это уже проверено (или проверяется)
        self._auto_probe()

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
        ed = self._connection_editor()
        if ed is None:
            return
        url, _auth_idx, token_raw, proxy = ed.peek()
        if not url:
            return
        self._last_probed_snapshot = ed.probe_snapshot()
        if onboarding.env_ref_status(url) == onboarding.ENV_REF_MISSING:
            # Резолв даст "" — стрелять пробой в пустой адрес нечестно: под
            # визуально заполненным полем проба соврала бы «нет соединения»/
            # «пустой адрес», как будто сломан сам мастер.
            self._probe_gen += 1   # обесценить летящую пробу
            self._set_conn_probe_line(Text(f"⚠ {onboarding.display_value(url)}", style=WARN))
            return
        if token_raw:
            auth = onboarding.authorization_header(token_raw)
        elif ed.token_cleared:
            auth = None                  # Ctrl+U: сохранённый стёрт ещё до записи
        else:
            auth = self._values.get("Authorization")
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
                result = self._run_connection_probe(probe_url, probe_auth, verify,
                                                    config_proxy)
            except Exception as exc:
                result = ProbeResult("unknown", None, None, 0, f"внутренняя ошибка проверки: {exc}")
            self.app.call_from_thread(self._on_auto_probe_done, result, gen)

        self.run_worker(_work, thread=True, exclusive=True, name="onboarding-conn-probe")

    def _run_connection_probe(self, url: str, auth, verify: bool, config_proxy):
        """Вердикт шага «Подключение» — той же differential-пробой, что в /settings:
        иначе мастер и настройки скажут про один сервер разное. Вынесен из воркера
        отдельным методом, чтобы проверяться без потока."""
        from tui.services.settings_probe import probe_connection
        return probe_connection(url, auth, verify, "", config_proxy=config_proxy,
                                probe_fn=self._probe_fn)

    def _on_auto_probe_done(self, result, gen: int) -> None:
        if gen != self._probe_gen or not self.is_mounted:
            return                       # протухший результат (правка/переход обесценили)
        from tui.services.probe_presentation import connection_line_text
        self._set_conn_probe_line(connection_line_text(result))

    def _set_conn_probe_line(self, text) -> None:
        """Единственный писатель строки вердикта — и потому единственное место, где
        заказывается подводка.

        Строка стоит ниже поля «Прокси», а тело шага длиннее вьюпорта рамки: без
        подводки Ctrl+Space отрабатывал за нижней кромкой, и человек читал это как
        «вообще ничего не происходит» (живой смок). Подводка обязана быть ЗДЕСЬ, а не
        при монтаже тела: тело не пересобирается, и вердикт приезжает обновлением на
        месте — второе нажатие осталось бы немым.

        `reveal`, а не ярус `KEEP_IN_VIEW`: строка — СООБЩЕНИЕ-итог, ответа не ждёт, и
        живёт до пересборки тела шага. Получив приоритет вопроса, она навсегда
        перебивала бы курсор формы (канон `form_frame`, тот же довод, что у
        `settings._singleton`)."""
        for w in self.query(".onb-conn-probe"):
            w.update(text)
            reveal(w)

    def _commit_config(self) -> bool:
        # Голосовые ключи идут той же ЕДИНОЙ точкой записи, что и остальные поля
        # мастера: `write_wizard_values` разбирает словарь на реестровые записи и
        # плоские якоря сама, а dotted-путь без якоря в `FIELD_ANCHOR_PATHS`
        # доезжает до файла как есть. Второй писатель здесь заводить нельзя —
        # порядок и атомарность записи держит она (см. её докстринг).
        values = dict(self._values)
        if self._voice_choice == "cloud":
            values["voice.url"] = self._voice_cloud["url"]
            # Пустой черновик токена значит «оставить сохранённый» (канон «Подключения» —
            # /settings._voice_cloud_save), а не «стереть»: без подстановки запись
            # стёрла бы рабочий секрет. Фолбэк — только здесь, у самой записи.
            # Ctrl+U — исключение из фолбэка: там пустота осознанная, и секрет уходит.
            values["voice.token"] = (
                self._voice_cloud["token"]
                or ("" if self._voice_token_cleared
                    else str(self._voice_raw_cloud().get("token") or "")))
            values["voice.model"] = self._voice_cloud["model"]
        try:
            missing = onboarding.write_wizard_values(
                self._config_path, values,
                deactivate_profile=self._deactivate_profile)
        except onboarding.PartialWriteError as exc:
            # Часть записи уже на диске — то же правило, что у ветки `missing` ниже:
            # нетронутый файл не обещаем. ЧТО именно легло, знает только писатель
            # (`written`): своя редакция состава называла реестр и там, где ушёл один
            # указатель профиля, — человек оставался уверен, что профиль активен,
            # пока следующий старт не шёл без него. Слова — общие с плоским мастером.
            self.query_one("#onb-body", Static).update(
                f"✗ остальное не записано: {exc} — при этом уже: "
                + ", ".join(_of.LANDED_WORDS.get(name, name) for name in exc.written))
            return False
        except (ValueError, OSError) as exc:
            # OSError — штатный отказ записи (диск, права, win-лок открытого
            # файла): мимо перехвата он ронял весь Textual (ревью L33).
            self.query_one("#onb-body", Static).update(f"✗ конфиг не записан: {exc}")
            return False
        if missing:
            # Непустой список значит, что ПЛОСКАЯ запись не состоялась целиком
            # («всё или ничего» — её правило). Записи реестра при этом уходят
            # ОТДЕЛЬНОЙ транзакцией и первыми, поэтому обещать «конфиг не записан»
            # нельзя: человек пошёл бы искать нетронутый файл, а там уже модели.
            # Называем ровно то, чего не хватает.
            self.query_one("#onb-body", Static).update(
                f"✗ не записаны поля: {', '.join(missing)} — впишите их в конфиг вручную")
            return False
        self.query_one("#onb-body", Static).update("✓ конфиг записан")
        return True

    def _commit(self) -> None:
        if not self._commit_config():
            return
        if self._voice_choice == "cloud":
            # Батарейка могла быть установлена раньше: managed-слой (config.local.yml)
            # перекрыл бы только что записанный юзерский voice — снимаем его, как это
            # делает /settings через voice_setup.wire_cloud (тот же канон).
            from voice_runtime.wire import unwire_voice
            try:
                unwire_voice()
            except Exception:
                pass
        self._install_selected()
        self._on_commit()
        if self._voice_choice == "local":
            self._start_voice_service()

    def _start_voice_service(self) -> None:
        """Голос работает сразу после мастера, без перезапуска: `_on_commit` уже
        перечитал конфиг (app.reload_config) — по нему и поднимаем сервис (установка
        только что переписала config.local.yml, гейт автостарта смотрит туда).

        Фоном и на воркере ПРИЛОЖЕНИЯ, а не экрана: после свежей установки порт
        заведомо мёртв, и `ensure_voice_service` блокирующе ждёт готовности (до 90 с —
        холодный старт грузит окружение и модель) — синхронный вызов заморозил бы финал
        мастера. Экран мастера в этот момент уже
        сменяется на TraceScreen (`_on_commit`), поэтому его виджеты трогать нельзя:
        результат сообщаем toast'ом приложения. Сбой (и погасшее приложение) не валит
        финал онбординга — как и установки выше."""
        app = self.app
        cfg = getattr(app, "_config", None) or {}

        def _work() -> None:
            from voice_runtime.autostart import ensure_voice_service
            try:
                ok = ensure_voice_service(cfg)
            except Exception:
                ok = False
            try:
                if ok:
                    app.call_from_thread(app.notify, "голосовой ввод готов — Ctrl+T")
                else:
                    app.call_from_thread(
                        app.notify,
                        "голосовой сервис не запустился — проверьте gc-voice status",
                        severity="warning")
            except Exception:
                pass          # приложение уже закрылось — некому показывать

        app.run_worker(_work, thread=True, name="onboarding-voice-start")

    def _install_selected(self) -> None:
        """Установки после подтверждения: MCP + локальный голосовой сервис.
        Шумные subprocess'ы корёжат alt-screen — app.suspend() (прогресс в обычный
        терминал, затем TUI возвращается). Per-item try/except: одна упавшая
        установка не валит остальные и не блокирует переход."""
        names = sorted(self._mcp_selected)
        want_voice = self._voice_choice == "local"
        if not names and not want_voice:
            return

        def _do() -> None:
            for name in names:
                try:
                    self._mcp.install(name)
                except Exception:
                    pass
            if want_voice:
                try:
                    self._voice_setup.switch_to_local()
                except Exception:
                    pass

        try:
            with self.app.suspend():
                parts = []
                if names:
                    parts.append("MCP: " + ", ".join(names))
                if want_voice:
                    parts.append("локальный голосовой сервис (~0.9 ГБ)")
                print("\nУстанавливаю " + "; ".join(parts) + " …\n")
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
            self._capture_voice_fields()   # черновик endpoint переживает уход назад
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
        elif step == STEP_VOICE:
            self._voice_advance()
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

        Шаг «Подключение» здесь наравне с помеченными по другой причине: печать на
        адресе двигает сам пресет-лист (канон 2026-08-18 — `ConnectionEditor`
        сворачивает список, как только совпадений не осталось, и раскрывает обратно,
        как только они появились), а футер шага именно от раскрытости списка и
        зависит (`_footer_text`)."""
        if self._step_id() in _MARK_STEPS or self._step_id() == STEP_CONNECTION:
            self._refresh_footer()

    def on_key(self, event: events.Key) -> None:
        step = self._step_id()
        if event.key == "ctrl+u" and step == STEP_CONNECTION:
            # Очистка сохранённого токена — та же клавиша, что в /settings.
            # Перехватываем здесь: Input держит на ней delete_left_all, а поле
            # токена пустое (секрет живёт в конфиге, в поле — маска).
            ed = self._connection_editor()
            if ed is not None and ed.clear_token():
                event.prevent_default()
                event.stop()
                self._refresh_footer()
            return
        if event.key == "ctrl+u" and self._voice_clear_token():
            # Голосовой шаг: та же клавиша и тот же смысл, что на «Подключении» выше.
            # Гейт («то ли поле, есть ли что стирать») живёт в редакторе — здесь только
            # маршрут, чтобы условие не разъехалось между двумя экранами.
            event.prevent_default()
            event.stop()
            self._refresh_footer()
            return
        # Раскрытый список поля под курсором забирает клавиши себе (канон /settings:
        # `is_open` проверяется первым) — иначе Esc выкинул бы из мастера, а стрелки
        # увели курсор с «Своего endpoint» прямо во время выбора. Списков в группе полей
        # два — модель и URL (спека 2026-08-05 §3) — и у обоих одна и та же механика:
        # какой из них раскрыт, решает ключ поля под фокусом, а не имя списка.
        ctrl = self._voice_focused_list()
        key = self._voice_focused_field_key()
        if ctrl is not None and ctrl.is_open:
            if event.key == "escape":
                event.stop()
                ctrl.close()               # `close` сам скажет строкой, чем вернуть
                self._refresh_footer()     # клавиши вернулись полю — футер тоже
                return
            if event.key in ("up", "down"):
                event.stop()
                ctrl.move(-1 if event.key == "up" else 1)
                return
            if event.key == "enter":
                event.stop()
                ed = self._voice_endpoint_editor()
                if ed is not None and key is not None:
                    ed.pick_in(key)             # подставляет в поле и сворачивает список
                self._capture_voice_fields()   # выбранное значение — в черновик шага
                self._refresh_footer()         # список свернулся — футер снова про шаг
                return
        if event.key in ("alt+down", "ctrl+down"):
            # Развод клавиш (спека 2026-08-06 §2): раскрыть/перечитать список поля под
            # курсором — единственная роль этой клавиши теперь, независимо от того,
            # есть ли на шаге проба вообще (см. ветку Ctrl+Space ниже — она про список
            # больше не спрашивает). Списков в мастере три: пресеты адреса
            # («Подключение»), модели агента («Модель» и под-формы уровней — тот же
            # редактор), модель/URL своего STT-endpoint («Голосовой ввод», спека
            # 2026-08-05 §3) — какой раскрыт, решает `ctrl` (см. `_voice_focused_list`
            # выше в теле метода).
            if ctrl is not None and ctrl.reload():
                event.stop()
                self._refresh_footer()     # список раскрылся — клавиши теперь его
                return
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
                _ed = self._connection_editor()
                if _ed is not None and _ed.reload_presets():
                    event.stop()
                    self._refresh_footer()   # клавиши теперь про список
                return
            return
        if event.key in ("ctrl+at", "ctrl+space", "ctrl+@"):
            # Развод §2 спеки 2026-08-06: клавиша больше не спрашивает список поля
            # (ушёл на Alt+↓ выше) — она либо проверяет связь (только шаг 0, карта проб
            # §2.5: на «Модели» и «Голосовом вводе» пробы нет вовсе), либо съедается
            # молча — не проваливается в глобальные биндинги, но и не обещает того,
            # чего на этих шагах нет (§5.1 спеки).
            if step != STEP_CONNECTION:
                event.stop()
                return
            # Конец цепочки — «перепроверить связь». Зовём _auto_probe НАПРЯМУЮ, мимо
            # гейта `_auto_probe_if_changed`: снимок гасит повтор автопробы на
            # неизменных полях, и без ручного пути вердикт «сервер недоступен»
            # залипал бы навсегда (§5.2).
            event.stop()
            ed = self._connection_editor()
            if ed is None:
                return
            url = ed.peek()[0]
            if not url:
                # _auto_probe на пустом адресе выходит ДО записи любой строки, и
                # клавиша осталась бы абсолютно немой. На чистой установке шаг
                # стартует именно с пустым адресом — там новичок и жмёт её первым
                # делом. Канон обоих доноров (`_start_draft_probe`, `_voice_probe`):
                # молчать в ответ на клавишу нельзя, объясняем, чего не хватает.
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
            # вернуть рассинхрон «фокус отдельно, курсор отдельно». Групп больше одной
            # пока только на шаге голоса — на остальных шагах клавиша просто молчит.
            event.stop()
            if self._step_id() == STEP_VOICE:
                self._move_group(1 if event.key == "tab" else -1)
            return
        if event.key == "escape" and step == STEP_CONNECTION:
            # Esc при раскрытом пресет-листе сворачивает ТОЛЬКО его (канон /settings) —
            # общий Esc ниже (выход из мастера) в этом случае не должен стрелять.
            ed = self._connection_editor()
            if ed is not None and ed.escape_presets():
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
            event.stop()
            ed = self._connection_editor()
            if ed is None:
                return
            leaving_id = getattr(self.focused, "id", None)
            ed.move(-1 if event.key == "up" else 1)
            if (leaving_id in ("conn-token", "conn-proxy")
                    or (leaving_id == "conn-url" and not ed.presets_open)):
                self._auto_probe_if_changed()   # уход с поля — авто-проба, если есть что проверять
            self._refresh_footer()   # состав клавиш зависит от того, раскрыт ли список
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
        if event.key in ("up", "down") and self._step_id() == STEP_VOICE:
            # Стрелки двигают курсор ВНУТРИ его группы (спека §4.2.2, приоритет
            # «раскрытый список → поля → варианты»): список забрал их выше, полям они
            # достаются, только когда курсор в них, — иначе строки режима переключались
            # бы прямо из-под набираемого поля.
            event.stop()
            delta = -1 if event.key == "up" else 1
            if self._voice_cursor_group() == _VOICE_GROUP_FIELDS:
                self._move_voice_field(delta)
            else:
                self._move_voice(delta)
            return
        if event.key == "space" and self._step_id() == STEP_VOICE:
            event.stop()
            self._voice_mark()
            return
        if event.key == "enter":
            event.stop()
            if step == STEP_CONNECTION:
                ed = self._connection_editor()
                if ed is not None and ed.enter_presets():
                    # пресет-лист открыт — подставил/свернул, не сабмит; подстановка
                    # пресета — тоже триггер авто-пробы (спека §5.5).
                    self._auto_probe_if_changed()
                    self._refresh_footer()   # список свернулся — футер снова про шаг
                    return
                self._submit_connection()
            elif step == STEP_MODEL:
                self._submit_model()
            elif step == STEP_LEVELS:
                self._submit_levels()
            elif step == STEP_VOICE:
                self._voice_enter()
            elif step == STEP_CONFIRM:
                self._commit()
            else:
                self._advance()
