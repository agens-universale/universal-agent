"""ModelEntryForm — форма ОДНОЙ записи реестра `models.<alias>` (профили, волна B).

Грамматика формы взята у `ConnectionEditor` целиком (контракт волны B, п.4):
подпись-`Static` над полем, `PasteInput` с рамкой, поле со списком — внутрь
рамки-группы (`.field-group`), позиционный курсор (`move`) с `None`-элементом на
строке-галке, секрет НЕ предзаполняется — действующий показан маской в
placeholder, контракт наружу — `peek()`/`is_dirty()`/`submit()`.

Форма НЕ пишет никуда и цели записи не знает: `submit()` отдаёт только те поля,
которые заданы явно, а merge со старой записью (и сохранность `pricing`/`backend`,
о которых форма не подозревает) — забота писателя `config_modules/registry_write`.
Пустое поле поэтому и значит «ключ не трогаем»: наследование от основного конфига
показано ЯВНО — placeholder называет конкретное значение, которое унаследуется
(токен — маской), а пресет-лист адреса первым пунктом предлагает «основной
конфиг», Enter по которому осознанно оставляет поле пустым (живая связь).

Две РАЗНЫЕ клавиши, и путать их нельзя (находка A4 ревью плана):
`Ctrl+Space` — проба соединения (`tui/services/settings_probe`, differential-вердикт
как в «Подключении»: контрольный запрос без токена и повтор с токеном только на
401); `Alt+↓` — подбор списка моделей с эндпоинта (сетевой источник строк, механика
`ModelEditor`: воркер-поток + поколение загрузки). Роли не пересекаются: проба
ничего не подбирает, подбор ничего не проверяет.
"""
from __future__ import annotations

from types import SimpleNamespace

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.widgets import Input, Static

import onboarding
from config_modules.model_registry import validate_model_alias
from onboarding_fields import (
    ADDRESS_HINT as _ADDRESS_HINT,
    NO_AUTH_FROZEN_PLACEHOLDER as _NO_AUTH_FROZEN_PLACEHOLDER,
    NO_AUTH_LABEL as _NO_AUTH_LABEL,
    NO_AUTH_TOGGLE_HINT as _NO_AUTH_TOGGLE_HINT,
    URL_ERROR as _URL_ERROR,
)
from tui.editors.field_list import (
    LIST_FAILED,
    LIST_LOADING,
    FieldListController,
    write_field,
)
from tui.editors.model import _plural_models
from tui.editors.preset_list import PRESET_HINT as _PRESET_HINT, preset_list_controller
from tui.palette import ACCENT, DEEP_DIM, INK, WARN
from tui.services.probe_presentation import probe_line_text
from tui.widgets.paste_input import PasteInput

# Хинты списков формы. Клавиши названы канон-словарём (спека 2026-08-06 §2.1):
# «перечитать список» — глагол сетевого источника, и он один в обоих состояниях
# списка, свёрнут тот или раскрыт. Сторож словаря — tests/tui/test_footer_contract.
MODEL_LIST_HINT = "модели с сервера · печать — фильтр/свой id · Enter — подставить · Esc — свернуть"
MODEL_LIST_HINT_NO_MATCH = "нет совпадений · Enter — оставить свой id · Esc — свернуть"
# Список ходит на сервер и раскрытым, и свёрнутым — поэтому «перечитать», а не
# «показать» (`field_list.LIST_CLOSED_HINT` — вариант локального источника).
MODEL_RELOAD_HINT = "Alt+↓ — перечитать список"

_ALIAS_LABEL = "Алиас"
_ALIAS_HINT = ("короткое имя записи — им пользуются слоты и привязки · "
               "пусто = имя из id")
# Имя занято записью ОСНОВНОГО конфига (§5 спеки эффективного вида): своей записи
# с таким именем в профиле нет, и общее «уже используется» увело бы от того, что
# на самом деле мешает, — отказ называет, ЧЕМ имя занято.
_ALIAS_TAKEN_BY_BASE = ("алиас занят записью основного конфига — "
                        "выберите другое имя")
# Алиас не обязателен (ревизия владельца): пустое поле показывает ПРИЗРАКОМ то
# конкретное имя, что реально возьмётся из id (placeholder = default_alias_for +
# уникализация, следует за полем id), и Enter принимает его без ввода. Пока id
# пуст и выводить не из чего — короткий принцип вместо длинной фразы: объяснение
# живёт в подсказке ПОД полем, а не в самом поле.
_ALIAS_PLACEHOLDER = "имя из id модели"
_ALIAS_OR_MODEL_EMPTY = "укажите id модели или алиас"
# Переименование — не «другая запись»: ссылки уровней и привязок едут следом
# одной транзакцией (см. `_submit_form` секции).
_ALIAS_RENAME_HINT = "переименование обновит уровни и привязки записи"
_MODEL_LABEL = "Провайдерский id модели"
_MODEL_PLACEHOLDER = "если не указать — id модели равен алиасу"
_URL_LABEL = "Адрес API-сервера"
_TOKEN_LABEL = "Токен"
_PH_KEEP = "если не указать — останется как сейчас"
_PH_NO_TOKEN = "если не указать — без токена"
# Первый пункт пресет-листа адреса — не «ещё один адрес», а РЕШЕНИЕ не задавать
# свой: поле остаётся пустым, связь живая (сменится адрес в основном конфиге —
# сменится и здесь). Адрес рядом с ним часто совпадает с адресом обычного
# пресета, и без слова «наследовать» строка читалась бы дублем: одинаковый url,
# разные последствия — литерал в файле против пустого поля (ревизия владельца
# по смоку).
_INHERIT_PRESET_NAME = "основной конфиг · наследовать"
_PROBE_RUNNING = "проверяю…"
_PROBE_NO_URL = "сначала укажите адрес — проверка стучится на него"
_LIST_NO_URL = "сначала укажите адрес — список приходит с него"


def _inherited(value: str) -> str:
    return f"наследуется: {value}"


class ModelEntryForm(Vertical):
    def __init__(self, *, initial: dict | None = None, existing_aliases=(),
                 fetch=None, probe=None, presets: list | None = None,
                 effective_url: str = "", effective_auth: str | None = None,
                 verify: bool = True, config_proxy: str | None = None,
                 base_aliases=frozenset()) -> None:
        super().__init__(classes="onb-dyn")
        initial = initial or {}
        # Эффективные адрес/Authorization основного конфига — то, что реально
        # унаследует запись с пустыми полями (решение 7 «наследование явно»):
        # ими же ходят проба и подбор списка, пока своих значений у записи нет.
        self._effective_url = effective_url or ""
        self._effective_auth = effective_auth
        # Алиасы записей основного конфига: занятое ими имя — не «дубль своей»
        # (своей такой нет), у него свой отказ, и призрак пустого алиаса обязан
        # уникализироваться против ЭФФЕКТИВНОГО набора имён, а не одних своих.
        self._base_aliases = frozenset(base_aliases)
        self._own_alias = initial.get("alias") or None
        self._other_aliases = set(existing_aliases) - (
            {self._own_alias} if self._own_alias else set())
        self._alias0 = self._own_alias or ""
        self._model0 = initial.get("model") or ""
        self._url0 = initial.get("url") or ""
        self._token_current = initial.get("token")   # только для маски placeholder
        self._no_auth = self._token_current == ""   # явное "" в записи = галка ●
        self._verify = verify
        self._config_proxy = config_proxy
        if fetch is None:
            from model_picker import fetch_models
            from tui.services.model_list_cache import make_cached_fetch
            fetch = make_cached_fetch(fetch_models)
        if probe is None:
            from tui.services.settings_probe import probe as default_probe
            probe = default_probe
        self._fetch = fetch
        self._probe = probe
        # Позиции курсора — порядок человека, настраивающего подключение (ревизия
        # владельца): куда → чем → что → как назвать. К входу в поле id адрес и
        # токен уже стоят — автоподгрузка списка идёт по ним. Алиас — обычное поле
        # и при правке (переименование — транзакция секции). `None` — строка-галка
        # «без авторизации», как в _positions ConnectionEditor: это не Input,
        # фокус с неё снимается явно.
        self._positions = ("mef-url", "mef-token", None, "mef-model", "mef-alias")
        self._idx = 0
        self._models: list = []
        # Ключ последней автоподгрузки — (url, auth), а не флаг «уже пробовали»:
        # ревизия владельца по смоку. Сбой при пустом токене, потом введённый
        # токен — вход в поле обязан сходить ЗАНОВО, флаг же держал форму на
        # первой неудаче до ручного Alt+↓. Повтор с ТЕМИ ЖЕ параметрами не
        # идёт: дребезга «ходьба по форме долбит сервер» не будет.
        self._autoload_key: tuple | None = None
        self._list_gen = 0                # поколение загрузки: протухший ответ отбрасывается
        self._probe_gen = 0
        # Оба контроллера заводятся ЗДЕСЬ, а не на монтировании: смонтированные поля
        # им нужны только в работе (`_field()` ищет по id лениво), а «ещё не заведён»
        # — состояние, о котором пришлось бы помнить каждому вызову хозяина.
        presets_items = list(presets) if presets is not None else []
        if self._effective_url:
            # Пункт-наследование первым и с РЕАЛЬНЫМ адресом основного конфига: Enter
            # на нём осознанно ОЧИЩАЕТ поле (`_apply_preset`), а пустое поле и значит
            # «наследовать». Запись с собственным адресом — не исключение, наоборот:
            # ей этот путь и нужен, другого явного возврата к наследованию нет
            # (решение 7). Над своим адресом список сам не раскрывается (канон
            # `can_open`) — там его раскрывает `reload_presets` (Alt+↓).
            presets_items.insert(0, SimpleNamespace(name=_INHERIT_PRESET_NAME,
                                                    url=self._effective_url, inherit=True))
        self._presets_ctrl = preset_list_controller(
            self, presets=presets_items, row_class="mef-preset-row", input_id="mef-url",
            hint=_PRESET_HINT, on_pick=self._apply_preset,
        )
        self._list = FieldListController(
            self, input_id="mef-model", row_class="mef-model-row",
            scroll_class="mef-model-scroll", hint_class="mef-model-hint",
            rows_source=lambda: list(self._models), render_row=self._model_row_text,
            matches=lambda m, q: q in m.id.lower(),
            value_of=lambda m: m.id, on_pick=self._apply_model,
            hint=lambda ok: MODEL_LIST_HINT if ok else MODEL_LIST_HINT_NO_MATCH,
            closed_hint=MODEL_RELOAD_HINT,
            # Печать в поле id — не только фильтр, но и ввод СВОЕГО имени модели
            # (сервер может её не отдавать): откатывать её по Esc нельзя — правило
            # поля адреса, а не языка (`preset_list`, `restore_on_close=False`).
            restore_on_close=False,
        )
        self._initial_snapshot = {
            "alias": self._alias0, "url": self._url0, "token": "", "model": self._model0,
        }

    # --- рендер ---
    def _is_edit(self) -> bool:
        """Правка существующей записи, а не создание новой. Пустое поле здесь значит
        «оставить прежнее» (merge писателя ключа не тронет), и обещать «наследуется»
        было бы неправдой — placeholder'ы это различают."""
        return self._own_alias is not None

    def _derived_alias(self, model: str) -> str:
        """Имя, которое реально возьмётся при пустом алиасе: логика мастера
        (`default_alias_for` + уникализация против чужих имён).

        Занятыми считаются и алиасы основного конфига (§5): предложить призраком
        имя, которое submit тут же отвергнет, значит соврать полем."""
        return onboarding.unique_alias(
            onboarding.default_alias_for(model),
            self._other_aliases | self._base_aliases)

    def _alias_placeholder(self, model: str) -> str:
        return self._derived_alias(model) if model else _ALIAS_PLACEHOLDER

    def _alias_hint(self) -> str:
        """Подсказка под полем алиаса — по тому, что клавиша здесь реально делает:
        правка → про переезд ссылок; создание → про правило имени."""
        return _ALIAS_RENAME_HINT if self._is_edit() else _ALIAS_HINT

    def _refresh_alias_placeholder(self) -> None:
        """Призрак в поле алиаса следует за полем id: печать в id меняет имя,
        которое возьмётся по умолчанию, — placeholder обязан говорить правду."""
        for w in self.query("#mef-alias"):
            w.placeholder = self._alias_placeholder(self._model_value())

    def _url_placeholder(self) -> str:
        """Placeholder поля адреса виден ровно в один момент — когда поле ПУСТО (в
        режиме правки это значит «стёрли, чтобы снять»), поэтому он говорит про
        пустоту, а не про сохранённое: пустое поле = наследование, и текст называет
        конкретный адрес, который начнёт действовать (эталон — маска прокси
        `connection.py:143-145`, где пустое поле тоже значит «снять»)."""
        if self._effective_url:
            return _inherited(self._effective_url)
        return _ADDRESS_HINT

    def _token_is_mask(self) -> bool:
        """placeholder токена сейчас — маска (своего секрета записи или
        унаследованного), а не подсказка: маркер `has-saved` красит её цветом
        значения (спека §3.4), и врать им нельзя."""
        return bool(self._token_current) or bool(self._effective_auth)

    def _token_placeholder(self) -> str:
        """Свой токен записи — маской (и никакого хинта: маска сама говорит
        «текущее значение остаётся»); своего нет — маска того, что реально
        унаследуется от основного конфига; нет и там — честное «без токена»."""
        if self._token_current:
            return onboarding.display_secret(self._token_current)
        if self._effective_auth:
            return _inherited(onboarding.display_secret(self._effective_auth))
        return _PH_KEEP if self._is_edit() else _PH_NO_TOKEN

    def _noauth_cls(self) -> str:
        sel = " selected" if self._positions[self._idx] is None else ""
        return "onb-option onb-dyn set-spaced" + sel

    def _noauth_text(self) -> Text:
        # Подсказка живёт В строке, а не в футере: Space — клавиша ровно этой строки,
        # в полях он печатает пробел (разбор подсказок в теле, ConnectionEditor).
        t = Text()
        t.append(("● " if self._no_auth else "○ ") + _NO_AUTH_LABEL)
        t.append("   " + _NO_AUTH_TOGGLE_HINT, style=DEEP_DIM)
        return t

    def _model_row_text(self, model, *, selected: bool) -> Text:
        t = Text()
        t.append("▸ " if selected else "  ", style=ACCENT)
        t.append(model.id)
        if getattr(model, "tool_capable", None) is False:
            t.append(" ⚠", style=WARN)
        return t

    def compose(self) -> ComposeResult:
        # Рамка одна и она всегда (канон §3.2.1): у поля со списком её рисует
        # группа, у одинокого поля — класс set-framed. Порядок полей — как в
        # `_positions`: адрес → токен → id → алиас.
        icls = "onb-dyn set-framed"
        yield Static(_URL_LABEL, classes="onb-hint onb-dyn")
        self._url_input = PasteInput(value="", placeholder=self._url_placeholder(),
                                     id="mef-url", classes="onb-dyn group-field")
        with Vertical(classes="field-group onb-dyn", id="mef-url-group"):
            yield self._url_input
        yield Static(_TOKEN_LABEL, id="mef-token-label",
                     classes="onb-hint onb-dyn set-group-gap")
        # has-saved: placeholder говорит одно из двух (маска действующего значения /
        # подсказка-объяснение), и различать их надо не вчитываясь (спека §3.4).
        tcls = icls + (" has-saved" if self._token_is_mask() else "")
        yield PasteInput(value="", placeholder=self._token_placeholder(), password=True,
                         id="mef-token", classes=tcls)
        yield Static(self._noauth_text(), id="mef-noauth", classes=self._noauth_cls())
        yield Static(_MODEL_LABEL, classes="onb-hint onb-dyn set-group-gap")
        # Поле id — со списком моделей (автоподгрузка при входе, Alt+↓ — перечитать),
        # значит внутри рамки-группы: рамка одна на поле и его список (спека §3.1).
        with Vertical(classes="field-group onb-dyn", id="mef-model-group"):
            yield PasteInput(value=self._model0, placeholder=_MODEL_PLACEHOLDER,
                             id="mef-model", classes="onb-dyn group-field")
        # Строка состояния списка (загрузка/сбой/счётчик) — пустая не занимает места
        # (`display`), как статусы `ModelEditor`.
        status = Static("", id="mef-model-status", classes="onb-hint onb-dyn")
        status.display = False
        yield status
        yield Static(_ALIAS_LABEL, classes="onb-hint onb-dyn set-group-gap")
        yield PasteInput(value=self._alias0,
                         placeholder=self._alias_placeholder(self._model0),
                         id="mef-alias", classes=icls)
        yield Static(self._alias_hint(), classes="onb-hint onb-dyn")
        yield Static(probe_line_text(None), id="mef-probe-line",
                     classes="set-hint set-dyn")
        # Строка ошибки монтируется сразу и пустой (display гасит место под неё):
        # remove()+mount() с тем же id в одном такте даёт DuplicateIds, если форма
        # падает на одной ошибке два submit() подряд (урок донора).
        error = Static("", id="mef-error", classes="onb-warn onb-dyn set-group-gap")
        error.display = False
        yield error

    def on_mount(self) -> None:
        self._sync_token_visibility()
        # Адрес — программной записью (`write_field`): обычное присваивание постит
        # `Input.Changed`, и пресет-лист сужался бы по собственному значению поля
        # (живой смок 2026-08-04 «Подключения»).
        write_field(self._url_input, self._url0)
        self._focus_position()

    # --- навигация ---
    def _sync_token_visibility(self) -> None:
        """● — поле не исчезает (раскладка не прыгает), а замораживается: disabled,
        пустое, placeholder «не используется». ○ — оживает с прежним placeholder."""
        inp = self.query_one("#mef-token", Input)
        inp.disabled = self._no_auth
        if self._no_auth:
            inp.value = ""
            inp.placeholder = _NO_AUTH_FROZEN_PLACEHOLDER
        else:
            inp.placeholder = self._token_placeholder()
        # Маркер живёт вместе с placeholder'ом: при ● текст «не используется» — не
        # маска, и поле не должно врать, что сохранённое значение есть.
        inp.set_class(not self._no_auth and self._token_is_mask(), "has-saved")

    def _skip_position(self, position) -> bool:
        """Позиция, на которой курсору делать нечего: замороженное (disabled) поле
        фокусировать нельзя, и стоять на нём — тупик без обратной связи. Заморозка у
        формы одна — поле токена под галкой «без авторизации», — но живёт она
        отдельным предикатом, а не веткой в `move`: там ей место среди навигации."""
        return position == "mef-token" and self._no_auth

    def move(self, delta: int) -> None:
        if self._list.move(delta) or self._presets_ctrl.move(delta):
            # Списки модальные: пока по раскрытому есть чем ходить, ↑/↓ водят ПО НЕМУ —
            # курсор формы остаётся на поле до Enter/Esc.
            return
        if self._list.is_open:
            # Фильтр не оставил строк: водить нечем, и держать клавишу себе значит
            # запереть стрелки без обратной связи (контракт `FieldListController.move`).
            self._list.close()
        if self._presets_ctrl.is_open:
            self._presets_ctrl.close()
        self._idx = (self._idx + delta) % len(self._positions)
        while self._skip_position(self._positions[self._idx]):
            self._idx = (self._idx + delta) % len(self._positions)
        self._focus_position()

    def _focus_position(self) -> None:
        """Приземление курсора: подсветка галки, раскрытие/подсказки списков, фокус."""
        target = self._positions[self._idx]
        if target == "mef-url":
            self._presets_ctrl.open()        # канон §2.1: вход на адрес — всегда раскрыть
        else:
            # Подсказка возврата не имеет права остаться висеть под чужим полем:
            # клавиша гейтится позицией курсора, и обещание уходит вместе с ней.
            self._presets_ctrl.hide_closed_hint()
        if target == "mef-model":
            self._list.show_closed_hint()    # чем перечитать список (Alt+↓)
            if self._models:
                # Список уже подгружен — раскрыть сразу (ревизия владельца: вход в
                # поле показывает варианты сам, Enter для этого не нужен).
                if not self._list.is_open:
                    self._list.open(forced=True, filter_text=self._model_value())
            else:
                # Автоподгрузка при входе — по ключу (адрес, авторизация): к этому
                # полю курсор приходит после них, идти есть по чему. Те же
                # параметры второй раз не перечитываем (дребезг), ИЗМЕНЁННЫЕ —
                # обязаны: сбой без токена и повтор с введённым токеном это один
                # сценарий, а не два. Сбой не блокирует ручной ввод, Alt+↓
                # остаётся принудительным перечитыванием.
                key = (self._current_url(), self._current_auth_header())
                if key != self._autoload_key:
                    self._autoload_key = key
                    self._start_models_fetch(refresh=False)
        else:
            self._list.hide_closed_hint()
            # Счётчик/ошибка списка — про поле, с которого курсор ушёл: «2 модели»
            # под чужим полем не про что.
            self._set_status("")
        row = self.query_one("#mef-noauth", Static)
        (row.add_class if target is None else row.remove_class)("selected")
        if target is None:
            # Галка — не Input: снять фокус явно, иначе прежний фокусный Input
            # «съедал» бы Space и печать.
            self.screen.set_focus(None)
        else:
            self.query_one(f"#{target}", Input).focus()

    def toggle_no_auth(self) -> bool:
        """Space на позиции галки: переключить «без авторизации». True — курсор был
        на галке (переключено); False — курсор в другом месте, клавишу хост НЕ стопит
        (пробел печатается в фокусное поле как обычно)."""
        if self._positions[self._idx] is not None:
            return False
        self._no_auth = not self._no_auth
        self.query_one("#mef-noauth", Static).update(self._noauth_text())
        self._sync_token_visibility()
        return True

    # --- списки: пресеты адреса и модели эндпоинта ---
    def _apply_preset(self, preset) -> None:
        """Enter по строке пресета. Пункт-наследование (duck-атрибут `.inherit`) —
        осознанно ОСТАВИТЬ поле пустым: подстановка адреса убила бы живую связь с
        основным конфигом (контракт `preset_list_controller`, `value_of`)."""
        field = self.query_one("#mef-url", Input)
        write_field(field, "" if getattr(preset, "inherit", False) else preset.url)
        self._invalidate_probe()

    def _apply_model(self, model_id: str) -> None:
        write_field(self.query_one("#mef-model", Input), model_id)
        # write_field глушит Input.Changed (иначе список сужался бы сам по себе) —
        # призрак алиаса пересчитываем руками, ему это событие и было нужно.
        self._refresh_alias_placeholder()
        self._invalidate_probe()

    def reload_presets(self) -> bool:
        """`Alt+↓` на поле адреса: раскрыть свёрнутый пресет-лист (канон локального
        источника — «показать список»). Свой адрес список сам не раскрывает
        (`can_open`), а вернуть его после Esc иначе нечем — и подсказка свёрнутого
        списка (`escape`) обещала бы мёртвую клавишу.

        `False` — курсор не на адресе либо список уже раскрыт: перечитывать у
        статического источника нечего."""
        if self._positions[self._idx] != "mef-url":
            return False
        return self._presets_ctrl.reload()

    def reload_models(self) -> bool:
        """`Alt+↓` на поле id: сходить на эндпоинт за списком моделей. Это ПОДБОР, не
        проба (роли клавиш разведены, контракт п.4).

        Сетевой источник строк: список идёт в сеть и раскрытым, и свёрнутым, поэтому
        клавиша всегда значит «перечитать» (`MODEL_RELOAD_HINT`) — раскрытие само по
        себе здесь ничего не значило бы. `False` — курсор не на поле id, клавиша не
        наша."""
        if self._positions[self._idx] != "mef-model":
            return False
        # Ручное перечитывание = автоподгрузка этими параметрами уже состоялась:
        # иначе уход с поля и возврат сходили бы в сеть ещё раз тем же ключом.
        self._autoload_key = (self._current_url(), self._current_auth_header())
        return self._start_models_fetch(refresh=True)

    def _start_models_fetch(self, *, refresh: bool) -> bool:
        """Уйти за списком моделей. `refresh=False` — автоподгрузка при входе в поле
        (кэш годится), `True` — принудительное перечитывание по Alt+↓."""
        url = self._current_url()
        if not url:
            self._set_status(_LIST_NO_URL)
            return True
        auth = self._current_auth_header()
        # Поколение бампаем ЗДЕСЬ: `exclusive=True` уже бегущий поток не
        # останавливает, и ответ прошлой загрузки перемонтировал бы список поверх
        # свежего (идиома `ModelEditor._start_fetch`).
        self._list_gen += 1
        gen = self._list_gen
        self._set_status(LIST_LOADING)
        # `group` задан явно: `exclusive` отменяет соседей ПО ГРУППЕ, а не по `name`,
        # и в общей группе «default» проба гасила бы летящий подбор и наоборот —
        # клавиши разведены по ролям, значит и по воркерам.
        self.run_worker(lambda: self._fetch_blocking(url, auth, gen, refresh), thread=True,
                        exclusive=True, group="model-entry-list",
                        name="model-entry-list-fetch")
        return True

    def _fetch_blocking(self, url: str, auth: str | None, gen: int,
                        refresh: bool) -> None:
        try:
            models = self._fetch(url, auth, verify=self._verify,
                                 config_proxy=self._config_proxy, refresh=refresh)
        except Exception:
            models = None
        self.app.call_from_thread(self._on_models, models, gen)

    def _on_models(self, models, gen: int) -> None:
        # Поколение протухло — ответ не про эту загрузку: не трогаем ни состояние,
        # ни DOM.
        if gen != self._list_gen or not self.is_mounted:
            return
        if self._positions[self._idx] != "mef-model":
            # Курсор успел уйти с поля: список запоминаем (вернётся — раскроется
            # сам), но раскрывать и вешать счётчик под чужим полем нельзя.
            self._models = list(models or [])
            return
        if not models:
            # Честная ошибка вместо молчания: сервер мог лежать, а клавиша выглядела
            # бы мёртвой. Вписать id руками при этом никто не мешает.
            self._models = []
            self._list.close()
            self._set_status(LIST_FAILED)
            return
        self._models = list(models)
        self._list.close()                   # прошлое раскрытие больше не наше
        self._list.open(forced=True, filter_text=self._model_value())
        n = len(self._models)
        self._set_status(f"{n} {_plural_models(n)}")

    def _set_status(self, text: str) -> None:
        """Строка состояния списка ПОД рамкой (§3.2.2: внутри рамки только выбор).
        Пустой текст = строки нет: `display` гасит и место под неё."""
        for w in self.query("#mef-model-status"):
            w.update(text)
            w.display = bool(text)

    def enter(self) -> bool:
        """Enter по раскрытому списку — подставить выделенное (True, клавиша наша);
        списков нет — False, и хозяин трактует Enter как «сохранить» (submit)."""
        if self._presets_ctrl.is_open:
            self._presets_ctrl.pick()        # подстановку делает `_apply_preset`
            return True
        if self._list.is_open:
            self._list.pick()
            return True
        return False

    def escape(self) -> bool:
        """Esc сворачивает СВОЁ под-состояние (раскрытый список) и съедает клавишу;
        сворачивать нечего — False, и Esc достаётся хозяину (тот закроет форму, а
        черновик умрёт вместе с ней). Порядок вложенности из Global Constraints."""
        if self._presets_ctrl.is_open:
            self._presets_ctrl.close()
            self._presets_ctrl.show_closed_hint()
            return True
        if self._list.is_open:
            self._list.close()
            self._set_status("")             # счётчик свёрнутого списка ни о чём
            self._list.show_closed_hint()
            return True
        return False

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "mef-model":
            self._list.apply_filter(event.input.value)   # no-op, если список свёрнут
            self._refresh_alias_placeholder()
        elif event.input.id == "mef-url":
            self._presets_ctrl.apply_filter(event.input.value)
        if event.input.id in ("mef-url", "mef-token", "mef-model"):
            self._invalidate_probe()

    def _invalidate_probe(self) -> None:
        """Значения, по которым ходила проба, изменились — прежний вердикт больше не
        про эту связь. Молчание честнее залипшего ✓; текст «не проверено» — тот же
        общий рендер, что и все прочие вердикты (`probe_line_text(None)`)."""
        self._probe_gen += 1                 # летящая проба тоже устарела
        self._set_probe_line(probe_line_text(None))

    # --- проба (Ctrl+Space) ---
    def _current_url(self) -> str:
        """Адрес пробы и списка: поле, иначе эффективный из основного конфига — то,
        по чему запись реально пойдёт (наследование)."""
        return self._field_value("mef-url") or self._effective_url

    def _current_auth_header(self) -> str | None:
        """Authorization тем же правилом, каким будет жить запись: ● — честно без
        токена; введённый текст; собственный токен записи; иначе унаследованный.
        Поле и проба обязаны говорить об одной и той же связи."""
        if self._no_auth:
            return None
        token_raw = self._field_value("mef-token")
        if token_raw:
            return onboarding.authorization_header(token_raw)
        if self._token_current:
            return onboarding.authorization_header(self._token_current)
        return self._effective_auth

    def probe_connection(self) -> bool:
        """`Ctrl+Space`: проверить связь по тем значениям, что стоят в полях СЕЙЧАС.
        Это ПРОБА, не подбор списка (контракт п.4).

        Вердикт differential, как в «Подключении»: контрольный запрос БЕЗ токена
        отвечает, требует ли сервер авторизацию вообще, и только на 401 идёт повтор
        С токеном — иначе «токен принят» говорилось бы там, где токен не проверяли.
        Смысл вердикта собирает `interpret_auth`, текст — общий `probe_line_text`
        (у него же модельный хвост: запись про конкретный id, и «доступен» без слова
        о самой модели было бы полуправдой)."""
        url = self._current_url()
        if not url:
            self._set_probe_line(Text(_PROBE_NO_URL, style=DEEP_DIM))
            return True
        auth = self._current_auth_header()
        model = self._model_value()
        self._probe_gen += 1
        gen = self._probe_gen
        self._set_probe_line(Text(_PROBE_RUNNING, style=DEEP_DIM))
        self.run_worker(lambda: self._probe_blocking(url, auth, model, gen), thread=True,
                        exclusive=True, group="model-entry-probe",
                        name="model-entry-probe")
        return True

    def _probe_blocking(self, url: str, auth: str | None, model: str, gen: int) -> None:
        from tui.services.settings_probe import ProbeResult, interpret_auth
        try:
            control = self._probe(url, None, self._verify, model,
                                  config_proxy=self._config_proxy)
            with_token = None
            if control.verdict == "rejected" and control.http_status == 401 and auth:
                with_token = self._probe(url, auth, self._verify, model,
                                         config_proxy=self._config_proxy)
            result = interpret_auth(control, with_token, bool(auth))
        except Exception as exc:
            result = ProbeResult("unknown", None, None, 0,
                                 f"внутренняя ошибка проверки: {exc}")
        self.app.call_from_thread(self._on_probe, result, model, gen)

    def _on_probe(self, result, model: str, gen: int) -> None:
        if gen != self._probe_gen or not self.is_mounted:
            return
        self._set_probe_line(probe_line_text(result, model_id=model))

    def _set_probe_line(self, text: Text) -> None:
        for w in self.query("#mef-probe-line"):
            w.update(text)

    # --- контракт для хозяина ---
    # Признаки состояния — read-only и БЕЗ сайд-эффектов: футер секции позиционный
    # (Enter значит разное на разных строках), а обещания клавиш зависят ещё и от
    # того, раскрыт ли список и есть ли в нём подсвеченная строка. Спросить это
    # действием нельзя — `reload_models()` уходит в сеть. Зеркало `ConnectionEditor`
    # (`on_url_field`/`presets_open`/`presets_have_selection`).
    @property
    def on_url_field(self) -> bool:
        """Курсор на адресе: только здесь работает `Alt+↓ — показать список`."""
        return self._positions[self._idx] == "mef-url"

    @property
    def on_model_field(self) -> bool:
        """Курсор на поле id: только здесь работает `Alt+↓ — перечитать список`."""
        return self._positions[self._idx] == "mef-model"

    @property
    def on_no_auth_row(self) -> bool:
        """Курсор на строке-галке: Space переключает её, и только на ней."""
        return self._positions[self._idx] is None

    @property
    def presets_open(self) -> bool:
        return self._presets_ctrl.is_open

    @property
    def list_open(self) -> bool:
        return self._list.is_open

    @property
    def presets_have_selection(self) -> bool:
        """В раскрытом списке есть подсвеченная строка — Enter подставит ИМЕННО её.
        Не то же, что «список не пуст»: форсированное раскрытие поверх своего адреса
        показывает все строки, не подсвечивая ни одной, — Enter там оставит своё."""
        return self._presets_ctrl.has_selection

    @property
    def list_has_selection(self) -> bool:
        return self._list.has_selection

    def _field_value(self, field_id: str) -> str:
        return self.query_one(f"#{field_id}", Input).value.strip()

    def _model_value(self) -> str:
        try:
            return self._field_value("mef-model")
        except NoMatches:
            return self._model0

    @property
    def original_alias(self) -> str | None:
        """Алиас, под которым запись живёт в файле СЕЙЧАС (None — создание).
        Секция сравнивает его с алиасом из `submit()`: разошлись — переименование,
        транзакция с переездом ссылок."""
        return self._own_alias

    def peek(self) -> dict:
        """Сырые значения полей без сайд-эффектов — для dirty-проверки и хозяина."""
        return {
            "alias": self._field_value("mef-alias"),
            "url": self._field_value("mef-url"),
            "token": self._field_value("mef-token"),
            "model": self._field_value("mef-model"),
        }

    def is_dirty(self) -> bool:
        try:
            values = self.peek()
        except NoMatches:
            return False
        if values != self._initial_snapshot:
            return True
        # Галка отличается от действующего варианта: «явно без авторизации» — такая
        # же правка, как текст в поле, хотя все поля стоят как были.
        return self._no_auth != (self._token_current == "")

    def _fail(self, message: str) -> tuple[bool, None, None]:
        for w in self.query("#mef-error"):
            w.update(message)
            w.display = True
        return False, None, None

    def _clear_error(self) -> None:
        for w in self.query("#mef-error"):
            w.update("")
            w.display = False

    def submit(self) -> tuple[bool, str | None, dict | None]:
        """(ok, alias|None, fields|None). `fields` — только те ключи, о которых форма
        что-то говорит; merge со старой записью делает писатель.

        Правило пустого поля читается по тому, БЫЛО ли в нём что-то при входе:

        * поле не заполняли (создание; в правке — токен, который не префиллится) —
          ключа нет, старое значение записи остаётся;
        * ПРЕДЗАПОЛНЕННОЕ поле стёрли — ключ `None`, то есть просьба удалить его
          (контракт писателя): запись возвращается к наследованию основного конфига.
          Промолчать здесь было бы враньём — форма показала бы «изменено», Enter
          ответил бы «сохранено», а в файле остался бы прежний адрес.

        Токен предзаполнить нельзя (секрет), поэтому его снятие ходит не через
        пустоту, а через галку «без авторизации»: ● — явное `""`, снятая ● — `None`.

        Ошибка остаётся В ФОРМЕ: сообщение под полями, `ok=False`, писать нечего."""
        values = self.peek()
        alias = values["alias"]
        if not alias:
            # Пустой алиас — имя из id (ровно то, что показывал призрак-placeholder):
            # одинаково при создании и правке — стереть алиас существующей записи
            # значит «переименуй в имя по умолчанию». Выводить не из чего только
            # когда пусто И поле id — честная ошибка.
            if not values["model"]:
                return self._fail(_ALIAS_OR_MODEL_EMPTY)
            alias = self._derived_alias(values["model"])
        try:
            validate_model_alias(alias)
        except ValueError as exc:
            return self._fail(str(exc))
        if alias in self._other_aliases:
            return self._fail(f"алиас '{alias}' уже используется")
        if alias in self._base_aliases:
            # Имя записи основного конфига: своей такой в профиле нет, дублем это не
            # является — но две строки реестра под одним алиасом читались бы как
            # одна, и отказ говорит, чем имя занято (§5).
            return self._fail(_ALIAS_TAKEN_BY_BASE)
        url = values["url"]
        if url and not onboarding._valid_endpoint_url(url):
            return self._fail(_URL_ERROR)
        self._clear_error()
        fields: dict = {}
        if values["model"]:
            fields["model"] = values["model"]
        elif self._model0:
            # Стёрли предзаполненный id: ключ снят — провайдерским id снова служит сам
            # алиас (`model_router`: `entry.get("model") or alias`).
            fields["model"] = None
        if url:
            fields["url"] = url
        elif self._url0:
            fields["url"] = None              # стёрли свой адрес — вернулись к наследованию
        if self._no_auth:
            if self._token_current != "":     # не «оставить прежнее ""», а изменение
                fields["token"] = ""
        elif values["token"]:
            fields["token"] = values["token"]
        elif self._token_current == "":
            # Галку сняли, своего токена не ввели: запись возвращается к наследованию —
            # ключ УДАЛЯЕТСЯ. Промолчать здесь значило бы оставить `token: ""` в файле
            # (пустое поле = «не трогаем») — переключатель работал бы в одну сторону.
            fields["token"] = None
        return True, alias, fields
