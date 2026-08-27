"""ModelsSectionEditor — тело секции «Модели» хаба /settings (волна B, решение 1).

Секция ПОГЛОЩАЕТ легаси-«Модель»: пустой реестр = сегодняшний редактор модели
целиком (`ModelEditor` правит `agent.model`) плюс вход в реестр; непустой — список
записей `models`, строки слотов `slots` (уровень → алиас), блок привязок `assign`
(потребитель → уровень или алиас) и строка «резервная модель». Образец архитектуры
— `McpSectionEditor`: своё состояние, свой рендер, экран только монтирует и роутит
клавиши.

**Формы одни — целевых файла два** (симметрия §1 спеки): тот же редактор с
`is_profile=True` становится под-видом секции «Профили» и правит ФАЙЛ ПРОФИЛЯ
(Task 6). Второго редактора нет; профильный режим меняет ровно три вещи, и все
три — следствия того, что за файл перед нами: валидатор записи (профильный —
запрет `profile:`, compat), отсутствие строки «резервная модель» (`agent.model`
— ключ config.yml, в профиле его нет вовсе, а значит нет и легаси-редактора, и
пустое состояние у профиля своё) и хвост футера (Esc возвращает к списку
профилей, а не в меню хаба).

Запись — НЕМЕДЛЕННО по действию, через `config_modules/registry_write` (решение 2):
секция вышла из save-машины `_EDITOR_SECTIONS`, submit-цикла с «изменено» у неё нет.
Показывается ФАЙЛОВОЕ состояние цели (решение 6) — редактируется файл; действующий
(merged) резолв показывают витрины (футер, `/config`). YAML editor не собирает
никогда: и формат, и валидация результата — забота слоя записи.

Клавиша `n`/`т` («добавить запись») живёт в ДВУХ написаниях, и это одна роль, не
две (прецедент — тихий синоним `Ctrl+↓` у `Alt+↓`): на списке фокуса в поле нет и
работает голая буква, а в пустом состоянии клавиатуру держит поле легаси-редактора
(в него печатают имя модели — «gemini», «nano»), поэтому там ту же роль несёт
`Alt+N`/`Alt+Т`. Футер называет ровно то написание, которое работает в текущем
состоянии.
"""
from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Rule, Static

from config_modules.model_registry import LEVEL_FALLBACK, LEVELS
from config_modules.model_sections_validation import SUBAGENT_NAMES
from tui.palette import ACCENT, DEEP_DIM, DIM, INK, WARN

# --- футеры секции (позиционные: Enter значит разное на разных строках) --------
#
# Регистрируются в стражах `tests/tui/test_footer_contract.py` этой же задачей
# (Global Constraints волны B): футер, не попавший в перебор, соврёт незамеченным.
# Разделитель — точка с одиночными пробелами (стиль MCP): канонический двойной
# зазор не влезал в две строки бюджета 64 колонок.
_FOOTER_ENTRY = ("↑/↓ · Enter — изменить · n — добавить · Del — удалить · "
                 "PgUp/PgDn — секции · Esc")
_FOOTER_RESERVE = "↑/↓ · Enter — изменить · n — добавить · PgUp/PgDn — секции · Esc"
# Пустое состояние: клавиатуру держит поле легаси-редактора, поэтому вход в реестр
# обещается модификаторным написанием (см. докстринг модуля).
_FOOTER_EMPTY = ("↑/↓ · Enter — сохранить · Alt+↓ — перечитать список · "
                 "Alt+N — добавить в реестр · PgUp/PgDn — секции · Esc")
# Под-форма резервной модели: свой путь сохранения (решение 3) — Enter списка занят
# реестром, а save-машины у секции больше нет.
_FOOTER_RESERVE_FORM = ("↑/↓ · Enter — сохранить · Alt+↓ — перечитать список · "
                        "Esc — к списку")
_FOOTER_FORM_URL = ("Enter — сохранить · Alt+↓ — показать список · "
                    "Ctrl+Space — проверить связь · Esc — к списку")
_FOOTER_FORM_MODEL = ("Enter — сохранить · Alt+↓ — перечитать список · "
                      "Ctrl+Space — проверить связь · Esc — к списку")
_FOOTER_FORM_NOAUTH = ("↑/↓ · Enter — сохранить · Space — переключить · "
                       "Ctrl+Space — проверить связь · Esc — к списку")
_FOOTER_FORM_FIELD = ("↑/↓ · Enter — сохранить · Ctrl+Space — проверить связь · "
                      "Esc — к списку")
_FOOTER_FORM_LIST = ("↑/↓ — выбор · Enter — подставить · Esc — свернуть · "
                     "Ctrl+Space — проверить связь")
_FOOTER_FORM_LIST_NO_MATCH = ("↑/↓ · Enter — оставить своё · Esc — свернуть · "
                              "Ctrl+Space — проверить связь")
_FOOTER_PICKER = "↑/↓ — выбор · Enter — выбрать · Esc — отмена"
_FOOTER_CONFIRM = "Enter — удалить · Esc — отмена"
# Нижняя половина секции: на строке слота значение — только алиас реестра, на
# строке привязки — ещё и уровень (индирекция через слоты). Удаления на обеих нет:
# снятие значения — первая позиция списка, а не Del (Global Constraints).
_FOOTER_SLOT = "↑/↓ · Enter — выбрать модель · n — добавить · PgUp/PgDn — секции · Esc"
_FOOTER_ASSIGN = ("↑/↓ · Enter — выбрать уровень или модель · n — добавить · "
                  "PgUp/PgDn — секции · Esc")
# Под-вид открытого профиля: клавиши те же, но Esc возвращает К СПИСКУ профилей, а
# не в меню хаба (вложенность из Global Constraints — её обязан назвать футер).
_EXIT_TO_LIST = "Esc — к списку"


def _to_list_exit(footer: str) -> str:
    """Тот же футер для под-вида профиля. ПРОИЗВОДНАЯ, а не вторая литеральная
    строка: список клавиш у двух режимов один, и дописанная в одном месте клавиша
    обязана появиться в обоих — иначе футер соврёт молчанием (тот же класс лжи, что
    стережёт `test_footer_contract`). Хвоста «· Esc» нет — это ошибка кода, а не
    краевой случай: молча вернуть исходную строку значило бы обещать выход в меню
    там, где его нет."""
    head, sep, tail = footer.rpartition(" · Esc")
    if not sep or tail:
        raise ValueError(f"футер без хвоста «· Esc» в под-вид не переводится: {footer}")
    return f"{head} · {_EXIT_TO_LIST}"


_FOOTER_PROFILE_ENTRY = _to_list_exit(_FOOTER_ENTRY)
_FOOTER_PROFILE_SLOT = _to_list_exit(_FOOTER_SLOT)
_FOOTER_PROFILE_ASSIGN = _to_list_exit(_FOOTER_ASSIGN)
# Пустой реестр профиля: строк нет вовсе, легаси-редактора у профиля не бывает —
# работает единственная клавиша входа в реестр.
_FOOTER_PROFILE_EMPTY = f"n — добавить запись · PgUp/PgDn — секции · {_EXIT_TO_LIST}"
# Тело под-вида ещё не смонтировано (первый кадр) — обещаем ровно то, что работает
# без него. Экран берёт эту строку ОТСЮДА, а не пишет свою: «куда ведёт Esc»
# формулируется в одном месте (импорт, не копия).
_FOOTER_PROFILE_BACK = _EXIT_TO_LIST

# --- тексты секции ------------------------------------------------------------
_RESERVE_LABEL = "резервная модель"
# Решение 6: shadow-hint легаси-секции («база наследования») НЕ переносится — при
# живом реестре `agent.model` базой наследования не является (§12.4).
_RESERVE_HINT = "действует только без реестра"
_RESERVE_UNSET = "не задана"
_EMPTY_HINT = (
    "Реестр моделей пуст — уровни light/default/max выключены, работает одна "
    "модель ниже.\n"
    "Добавьте модель в реестр, чтобы включить уровни и привязки потребителей."
)
# Пустое состояние ПРОФИЛЯ — своё: «модели ниже» у него нет (резервная модель живёт
# в config.yml). Говорит РОВНО про реестр и ни слова про файл целиком: профиль без
# своих `models` вполне может переопределять `slots`/`assign` (и строка списка это
# показывает) — называет их превью оверлеев. Самодостаточно и на «вы» — канон
# пустых состояний (Global Constraints).
_PROFILE_EMPTY_HINT = (
    "Своего реестра моделей у профиля нет — модели действуют из основного конфига.\n"
    "Добавьте запись клавишей n, чтобы профиль переопределил и их."
)
_NO_ADDRESS_HINT = ("адрес не задан ни у записи, ни в «Подключении» — "
                    "по такой записи запрос не уйдёт")
_PICKER_DEFAULT_HINT = ("в реестре станет больше одной модели — выберите, какая "
                        "работает по умолчанию")
_DEFAULT_TAIL = "← default"

# --- нижняя половина секции: слоты (уровень → алиас) и привязки потребителей ---
_SLOT_UNSET = "не задан"
_ASSIGN_ACTIVE = "активная"
_PICKER_SLOT_HINT = "первая строка снимает слот — уровень вернётся к фоллбэку"
_PICKER_ASSIGN_HINT = ("первая строка снимает привязку — потребитель уйдёт на "
                       "активную модель")
_SLOT_DEAD_HINT = "уровень не резолвится: задайте хотя бы один слот"
_ASSIGN_ACTIVE_HINT = "работает активной моделью сессии, как основной трейс"
# `auto_judge` — потребитель волны C: строка в реестре есть, но никто его ещё не
# зовёт. Фоллбэк у него СВОЙ (уровень default, не активная модель — см. коммент
# `_CONSUMER_RU_LABEL` и `ModelRouter.resolve`), поэтому общий текст «активной» ему
# не подходит.
_AUTO_JUDGE_HINT = "активен с функцией авто-выбора"
_AUTO_JUDGE_UNBOUND_HINT = "без привязки идёт уровнем default, не активной моделью"

# Подписи потребителей, которых НЕТ в транспортном словаре `_CONSUMER_RU_LABEL`
# (`utils/llm_chat_client.py`): тот заведён для notice деградации и остаётся его
# собственностью — здесь только UI-добавка (контракт п.2 волны B).
_UI_CONSUMER_LABEL = {
    "auto_judge": "судья auto_judge",
    "*": "* · остальные субагенты",
}
# Роль субагента рядом с его именем — грамматика «имя · описание» канона
# `_mode_row_text`: имя ключа в файле человеку нужно (он его там и увидит),
# описание отвечает «а это кто».
_SUBAGENT_ROLE = {
    "subagent_worker": "исполнительские шаги",
    "subagent_code_generator": "генерация кода",
    "subagent_code_planner": "планирование кода",
    "subagent_planner": "общее планирование",
    "subagent_critic": "критика решений",
    "subagent_code_reviewer": "ревью кода",
    "subagent_special_agent": "агент по описанию",
}


def assign_consumers() -> list:
    """Строки блока привязок в порядке спеки §5.1: служебные потребители, затем
    субагенты ПОИМЁННО и wildcard «остальные субагенты» последним.

    Оба списка — из реестров кода (`ModelRouter.CONSUMERS`, `SUBAGENT_NAMES`), не
    копии в UI: новый потребитель приезжает строкой сам. `main` из реестра
    вычитается — основной трейс управляется уровнем, а не привязкой, и схема ключ
    `assign.main` прямо запрещает."""
    from model_router import CONSUMERS
    return ([c for c in CONSUMERS if c != "main"]
            + [f"subagents.{name}" for name in SUBAGENT_NAMES]
            + ["*"])


def consumer_labels() -> dict:
    """Подпись каждой строки привязок: 6 готовых берутся ИЗ транспортного словаря
    (копия разошлась бы с ним на первой же правке), остальные — свои."""
    from utils.llm_chat_client import _CONSUMER_RU_LABEL
    labels = dict(_CONSUMER_RU_LABEL)
    labels.update(_UI_CONSUMER_LABEL)
    for name in SUBAGENT_NAMES:
        # `.get`, а не индекс: новый субагент в коде появляется строкой сам — пусть
        # витрина деградирует до общей подписи, а не роняет секцию `KeyError`.
        labels[f"subagents.{name}"] = f"{name} · {_SUBAGENT_ROLE.get(name, 'субагент')}"
    return labels


def slot_head(level: str) -> str:
    """Голова строки слота — «уровень · роль» копирайтом §1 из ЕДИНОГО источника
    (`tui/toggles.py`, там же его берут хинты `/level`)."""
    from tui.toggles import _LEVEL_ROLE_TEXT
    return f"{level} · {_LEVEL_ROLE_TEXT.get(level, '')}"


def value_style(value: str) -> str:
    """Цвет ЗАДАННОГО значения слота/привязки — §12.4: уровень — цветом уровня из
    `TOGGLES["level"]` (единый источник, канон `main.py::_level_color` — будущий
    авто-режим со своим цветом подхватится сам), алиас-пин — DIM.

    Отсутствие значения («не задан»/«активная» — DEEP_DIM) сюда не попадает: это не
    значение, и решает его тот, кто ЗНАЕТ факт (ключа в файле нет / это первая
    позиция списка), а не сравнение с подписью — алиас, названный «не задан»,
    схемой разрешён (`validate_model_alias` режет только `RESERVED_WORDS`). Алиас с
    именем уровня, наоборот, невозможен — они и есть `RESERVED_WORDS`."""
    if value in LEVELS:
        from tui.toggles import TOGGLES
        return {v.value: v.color for v in TOGGLES["level"].values}.get(value, INK)
    return DIM


def entry_address(entry, inherited_url: str) -> str:
    """Адрес, по которому реально пойдёт запись реестра: свой `url`, точечный оверлей
    `backend.completions_url`, иначе унаследованный от основного конфига — та же
    цепочка, которой резолвит `ModelRouter`. Пусто — запись нерабочая.

    Владелец цепочки один на секцию и на глиф хаба: витрина и вердикт не имеют права
    расходиться (реестр из одной записи без адреса показывался «✓» на хабе и гас в
    секции — находка ревью Task 3)."""
    if not isinstance(entry, dict):
        entry = {}
    backend = entry.get("backend") if isinstance(entry.get("backend"), dict) else {}
    return entry.get("url") or backend.get("completions_url") or inherited_url or ""


def registry_resolves_any_address(document: dict, inherited_url: str) -> bool:
    """Хоть одна запись реестра резолвит адрес. Ноль — реестр честно валиден по схеме,
    но не работает: `url` схемой не требуется, а фаза 2 проверяет только ссылочную
    целостность."""
    models = document.get("models") or {}
    return any(entry_address(entry, inherited_url) for entry in models.values())


def registry_preview(document: dict) -> str:
    """Превью хаба для непустого реестра: «N в реестре · default <алиас>».

    Резервная модель (`agent.model`) в счёт N НЕ входит (§12.4) — она не запись
    реестра, а фоллбэк на случай пустого реестра."""
    models = document.get("models") or {}
    slots = document.get("slots") or {}
    text = f"{len(models)} в реестре"
    default = slots.get("default")
    if default:
        text += f" · default {default}"
    return text


class ModelsSectionEditor(Vertical):
    """Тело секции. Состояния (взаимоисключающие, каждое — своё под-состояние Esc):

    * список записей, слотов и привязок (или пустое состояние с легаси-редактором)
      — база;
    * `ModelEntryForm` — создание/правка записи реестра;
    * `ModelEditor` — под-форма резервной модели (`agent.model`);
    * инлайн-пикер значения — раскрытый список под строкой слота/привязки, а также
      вопрос «какая модель по умолчанию» поверх формы перед записью (один механизм,
      решение 9);
    * confirm-баннер удаления.
    """

    def __init__(self, *, target, template: str, is_profile: bool = False,
                 effective_url: str = "",
                 effective_auth: str | None = None, verify: bool = True,
                 config_proxy: str | None = None, presets=(),
                 legacy_editor_factory=None, legacy_hints=(),
                 on_change=None, on_notice=None, on_legacy_save=None) -> None:
        super().__init__(classes="onb-dyn")
        self._target = target
        self._template = template
        # Что за файл перед нами — единственное, чем режимы отличаются: валидатор
        # записи, отсутствие `agent.model` и хвост футера следуют отсюда.
        self._is_profile = is_profile
        self._effective_url = effective_url or ""
        self._effective_auth = effective_auth
        self._verify = verify
        self._config_proxy = config_proxy
        self._presets = list(presets)
        self._legacy_editor_factory = legacy_editor_factory
        self._legacy_hints = list(legacy_hints)
        self._on_change = on_change
        self._on_notice = on_notice
        self._on_legacy_save = on_legacy_save
        self._document = self._read_document()
        self._idx = 0                     # курсор строки списка (записи + резервная)
        self._form = None                 # ModelEntryForm в работе
        # Легаси-редактор — СОСТОЯНИЕ (флаг), а виджет пересоздаётся каждой сборкой
        # тела: снесённый Textual'ом виджет обратно не монтируется, и переиспользование
        # ссылки давало пустое тело секции.
        self._legacy_open = self._legacy_default()
        self._legacy = None
        self._picker = None               # {"items", "idx", "on_pick"}
        self._pending = None              # {"alias", "references", "last"} — confirm удаления

    # --- файловое состояние цели ---------------------------------------------
    def _read_document(self) -> dict:
        import config_raw
        return config_raw.read_document(self._target)

    @property
    def _entries(self) -> dict:
        models = self._document.get("models")
        return dict(models) if isinstance(models, dict) else {}

    @property
    def _slots(self) -> dict:
        slots = self._document.get("slots")
        return dict(slots) if isinstance(slots, dict) else {}

    @property
    def _assign(self) -> dict:
        assign = self._document.get("assign")
        return dict(assign) if isinstance(assign, dict) else {}

    @property
    def _aliases(self) -> list:
        return list(self._entries)

    @property
    def registry_empty(self) -> bool:
        return not self._entries

    def _legacy_default(self) -> bool:
        """Состояние легаси-редактора «по умолчанию» для текущего файла: пустой реестр
        config.yml — это он и есть (секция поглотила легаси-«Модель»), а у профиля его
        не бывает вовсе. Решает ОДНО место: тот же вопрос задают конструктор, `_reload`
        и выход из формы, и разойтись им нечем."""
        return self.registry_empty and not self._is_profile

    def _reserve_model(self) -> str:
        import config_raw
        value = config_raw.value_at(self._document, "agent.model")
        return value if isinstance(value, str) else ""

    def _entry_address(self, alias: str) -> str:
        return entry_address(self._entries.get(alias), self._effective_url)

    def _slot_value(self, level: str):
        """Значение слота из ФАЙЛА цели либо None («не задан»)."""
        value = self._slots.get(level)
        return value if isinstance(value, str) and value else None

    def _assign_value(self, consumer: str):
        """Значение привязки из ФАЙЛА цели либо None («активная»): у субагентов оно
        лежит вложенным словарём, у служебных — плоским ключом."""
        assign = self._assign
        if consumer == "*" or consumer.startswith("subagents."):
            name = "*" if consumer == "*" else consumer.split(".", 1)[1]
            nested = assign.get("subagents")
            value = nested.get(name) if isinstance(nested, dict) else None
        else:
            value = assign.get(consumer)
        return value if isinstance(value, str) else None

    # --- строки списка --------------------------------------------------------
    def _rows(self) -> list:
        """Строки списка: записи реестра, слоты уровней, привязки потребителей и
        резервная модель. В пустом состоянии строк нет вовсе — там курсор держит
        легаси-редактор (у профиля — своё пустое состояние), а слотам и привязкам
        без реестра управлять нечем.

        «Резервной модели» у профиля нет: это ключ `agent.model` файла config.yml
        (§12.4), и рисовать его строку поверх чужого файла значило бы обещать
        правку того, чего в нём не существует."""
        if self.registry_empty:
            return []
        return ([{"kind": "entry", "alias": a} for a in self._aliases]
                + [{"kind": "slot", "level": level} for level in LEVELS]
                + [{"kind": "assign", "consumer": c} for c in assign_consumers()]
                + ([] if self._is_profile else [{"kind": "reserve"}]))

    def _row_at_cursor(self) -> dict | None:
        rows = self._rows()
        if not rows:
            return None
        self._idx = min(max(self._idx, 0), len(rows) - 1)
        return rows[self._idx]

    def _alias_width(self) -> int:
        """Колонка выравнивается по тому, что на экране ЕСТЬ: у профиля строки
        «резервная модель» нет, и её подпись колонку не расширяет."""
        labels = [] if self._is_profile else [_RESERVE_LABEL]
        return max([len(a) for a in self._aliases] + [len(label) for label in labels] + [0])

    def _entry_row_text(self, alias: str, selected: bool) -> Text:
        entry = self._entries.get(alias)
        entry = entry if isinstance(entry, dict) else {}
        dead = not self._entry_address(alias)
        t = Text()
        t.append("▸ " if selected else "  ", style=ACCENT)
        t.append(f"{alias:<{self._alias_width()}}", style=DEEP_DIM if dead else INK)
        t.append("  ")
        t.append(str(entry.get("model") or alias), style=DEEP_DIM if dead else DIM)
        if self._slots.get("default") == alias:
            t.append("  " + _DEFAULT_TAIL, style=DIM)
        return t

    def _reserve_row_text(self, selected: bool) -> Text:
        model = self._reserve_model()
        t = Text()
        t.append("▸ " if selected else "  ", style=ACCENT)
        t.append(f"{_RESERVE_LABEL:<{self._alias_width()}}", style=INK)
        t.append("  ")
        t.append(model or _RESERVE_UNSET, style=DIM if model else DEEP_DIM)
        t.append("  " + _RESERVE_HINT, style=DEEP_DIM)
        return t

    def _binding_columns(self) -> tuple:
        """(label_w, value_w) — общие колонки нижней половины секции: слоты и
        привязки живут одной сеткой, значения стоят друг под другом. Формулы — канон
        `_modes_columns` (`settings.py`): +3 зазор, +2 на курсор, ширина значения по
        макс. длине ВСЕХ имён, которые строка может показать."""
        labels = consumer_labels()
        heads = [slot_head(level) for level in LEVELS]
        heads += [labels[consumer] for consumer in assign_consumers()]
        values = [_SLOT_UNSET, _ASSIGN_ACTIVE, *LEVELS, *self._aliases]
        return max(len(h) for h in heads) + 3 + 2, max(len(v) for v in values)

    def _binding_row_text(self, head: str, value, unset_label: str,
                          selected: bool) -> Text:
        """Строка слота/привязки — грамматика `_mode_row_text`: курсор, голова
        выровненным блоком INK, колонка значения своим цветом (§12.4). Трека здесь
        нет — список значений произвольный (алиасы реестра), фиксированных позиций
        под него не существует.

        `value is None` — ключа в файле нет: подпись `unset_label` и DEEP_DIM по
        ФАКТУ отсутствия, а не по совпадению значения с этой подписью."""
        label_w, value_w = self._binding_columns()
        shown = unset_label if value is None else value
        t = Text()
        t.append("▸ " if selected else "  ", style=ACCENT)
        t.append(head.ljust(label_w - 2), style=INK)
        t.append("  ")
        t.append(shown.ljust(value_w),
                 style=DEEP_DIM if value is None else value_style(value))
        return t

    def _slot_row_text(self, level: str, selected: bool) -> Text:
        return self._binding_row_text(
            slot_head(level), self._slot_value(level), _SLOT_UNSET, selected)

    def _assign_row_text(self, consumer: str, selected: bool) -> Text:
        return self._binding_row_text(
            consumer_labels()[consumer], self._assign_value(consumer),
            _ASSIGN_ACTIVE, selected)

    def _row_text(self, row: dict, selected: bool) -> Text:
        if row["kind"] == "reserve":
            return self._reserve_row_text(selected)
        if row["kind"] == "slot":
            return self._slot_row_text(row["level"], selected)
        if row["kind"] == "assign":
            return self._assign_row_text(row["consumer"], selected)
        return self._entry_row_text(row["alias"], selected)

    def _slot_hint(self, level: str) -> str:
        """Чем слот занят, а если не задан — что работает ВМЕСТО него: честный
        фоллбэк по матрице `LEVEL_FALLBACK` (тот же обход, что у `ModelRouter.
        _resolve_level`, включая правило «единственная модель реестра»), а не общая
        фраза «действует default» — она была бы враньём, когда default тоже пуст."""
        alias = self._slot_value(level)
        if alias:
            return f"уровень {level} работает моделью «{alias}»"
        for candidate in LEVEL_FALLBACK[level][1:]:
            fallback = self._slot_value(candidate)
            if fallback:
                return f"{_SLOT_UNSET} · действует {candidate} → {fallback}"
        if len(self._aliases) == 1:
            return (f"{_SLOT_UNSET} · действует единственная модель реестра → "
                    f"{self._aliases[0]}")
        return f"{_SLOT_UNSET} · {_SLOT_DEAD_HINT}"

    def _assign_hint(self, consumer: str) -> str:
        value = self._assign_value(consumer)
        if value is None:
            base = _AUTO_JUDGE_UNBOUND_HINT if consumer == "auto_judge" else _ASSIGN_ACTIVE_HINT
        elif value in LEVELS:
            base = f"работает уровнем {value}"
        else:
            base = f"жёсткий пин модели «{value}»"
        if consumer == "auto_judge":
            return f"{_AUTO_JUDGE_HINT} · {base}"
        return base

    def _hint_text(self) -> str:
        """Подсказка под списком — про строку под курсором (канон «Профилей»):
        куда пишем и чем строка проблемна."""
        row = self._row_at_cursor()
        if row is None:
            return ""
        target = Path(self._target).name
        if row["kind"] == "reserve":
            return f"{_RESERVE_HINT} · запись: {target}"
        if row["kind"] == "slot":
            return f"{self._slot_hint(row['level'])} · запись: {target}"
        if row["kind"] == "assign":
            return f"{self._assign_hint(row['consumer'])} · запись: {target}"
        alias = row["alias"]
        address = self._entry_address(alias)
        if not address:
            return f"{_NO_ADDRESS_HINT} · запись: {target}"
        own = (self._entries.get(alias) or {}).get("url")
        head = address if own else f"наследует адрес основного конфига: {address}"
        return f"{head} · запись: {target}"

    # --- рендер ---------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield from self._body()

    def _body(self):
        if self._form is not None:
            yield self._form
            return
        if self._legacy_open:
            if self.registry_empty:
                yield Static(_EMPTY_HINT, classes="set-hint set-dyn", markup=False)
            else:
                yield Static(f"{_RESERVE_LABEL} · {_RESERVE_HINT}",
                             classes="onb-hint onb-dyn", markup=False)
            self._legacy = self._make_legacy_editor()
            yield self._legacy
            yield from self._legacy_hint_widgets()
            return
        rows = self._rows()
        if not rows:
            # Пустой реестр профиля: легаси-редактора, который держал бы это место в
            # config.yml, здесь не бывает — своё пустое состояние с входом в реестр.
            yield Static(_PROFILE_EMPTY_HINT, classes="set-hint set-dyn", markup=False)
            return
        previous = None
        for i, row in enumerate(rows):
            # Черта на СМЕНЕ группы (записи → слоты → привязки → резервная) — то же
            # правило, что у «Режимов работы»/«Профилей»: группы разделены, внутри
            # группы строки идут сплошняком.
            if previous is not None and row["kind"] != previous:
                yield Rule(classes="set-group-sep")
            previous = row["kind"]
            cls = "models-row set-row set-dyn set-spaced" + (" selected" if i == self._idx else "")
            yield Static(self._row_text(row, i == self._idx), classes=cls, markup=False)
        yield Static(self._hint_text(), classes="set-hint set-dyn models-hint", markup=False)

    def _legacy_hint_widgets(self):
        for text, cls in self._legacy_hints:
            yield Static(text, classes=cls, markup=False)

    def _make_legacy_editor(self):
        """Легаси-редактор строит ЭКРАН (он знает про резолвнутые адрес/токен и про
        происхождение поля), а секция решает, когда он на сцене, — и просит новый
        экземпляр на каждую сборку тела."""
        return self._legacy_editor_factory()

    def _rebuild(self) -> None:
        """Пересобрать тело под текущее состояние. Снос + монтаж в один такт легален
        ровно потому, что состояния не делят id (форма — `mef-*`, легаси-редактор —
        `onb-model*`), а строки списка id не имеют вовсе: DuplicateIds ловит
        столкновение в ОДНОМ родителе (урок `_singleton` экрана)."""
        self.remove_children()
        for widget in self._body():
            self.mount(widget)

    def _refresh_rows(self) -> None:
        if self._form is not None or self._legacy_open:
            return          # на экране под-форма — строк списка нет и быть не должно
        rows = self._rows()
        widgets = list(self.query(".models-row"))
        if len(widgets) != len(rows):
            self._rebuild()
            return
        for i, (widget, row) in enumerate(zip(widgets, rows)):
            widget.update(self._row_text(row, i == self._idx))
            widget.set_class(i == self._idx, "selected")
        for widget in self.query(".models-hint"):
            widget.update(self._hint_text())
        # Подсвеченную строку подводим сами (канон `field_list`/`sessions`): фокуса у
        # строк нет, за курсором Textual не следит, а список секции длиннее экрана —
        # записи, три слота и полтора десятка привязок.
        if 0 <= self._idx < len(widgets):
            self.call_after_refresh(widgets[self._idx].scroll_visible, animate=False)

    def _reload(self) -> None:
        """Файл изменился нами же — перечитать состояние цели и пересобрать тело.
        Опустевший реестр возвращает секцию в пустое состояние (легаси-редактор), а
        наполнившийся — в список: состояние секции задаёт ФАЙЛ, а не история нажатий."""
        self._document = self._read_document()
        self._legacy_open = self._legacy_default()
        rows = self._rows()
        self._idx = min(self._idx, max(len(rows) - 1, 0))
        self._rebuild()

    # --- состояние наружу (для позиционного футера экрана) --------------------
    @property
    def awaiting_confirm(self) -> bool:
        return self._pending is not None

    @property
    def picker_open(self) -> bool:
        return self._picker is not None

    @property
    def form_open(self) -> bool:
        return self._form is not None

    @property
    def legacy_open(self) -> bool:
        """Легаси-редактор на экране: пустое состояние либо под-форма резервной."""
        return self._legacy_open

    def footer_text(self) -> str:
        if self._pending is not None:
            return _FOOTER_CONFIRM
        if self._picker is not None:
            return _FOOTER_PICKER
        if self._form is not None:
            return self._form_footer()
        if self._legacy_open:
            return _FOOTER_EMPTY if self.registry_empty else _FOOTER_RESERVE_FORM
        row = self._row_at_cursor()
        if row is None:
            return _FOOTER_PROFILE_EMPTY    # строк нет только у профиля (см. `_body`)
        if row["kind"] == "reserve":
            return _FOOTER_RESERVE
        if row["kind"] == "slot":
            return _FOOTER_PROFILE_SLOT if self._is_profile else _FOOTER_SLOT
        if row["kind"] == "assign":
            return _FOOTER_PROFILE_ASSIGN if self._is_profile else _FOOTER_ASSIGN
        return _FOOTER_PROFILE_ENTRY if self._is_profile else _FOOTER_ENTRY

    def _form_footer(self) -> str:
        form = self._form
        if form.presets_open:
            return (_FOOTER_FORM_LIST if form.presets_have_selection
                    else _FOOTER_FORM_LIST_NO_MATCH)
        if form.list_open:
            return (_FOOTER_FORM_LIST if form.list_has_selection
                    else _FOOTER_FORM_LIST_NO_MATCH)
        if form.on_url_field:
            return _FOOTER_FORM_URL
        if form.on_model_field:
            return _FOOTER_FORM_MODEL
        if form.on_no_auth_row:
            return _FOOTER_FORM_NOAUTH
        return _FOOTER_FORM_FIELD

    # --- клавиши --------------------------------------------------------------
    def move(self, delta: int) -> None:
        if self._pending is not None:
            return
        if self._picker is not None:
            items = self._picker["items"]
            self._picker["idx"] = (self._picker["idx"] + delta) % len(items)
            self._refresh_picker_rows()
            return
        if self._form is not None:
            self._form.move(delta)
            return
        if self._legacy_open and self._legacy is not None:
            self._legacy.move(delta)
            return
        rows = self._rows()
        if not rows:
            return
        self._idx = (self._idx + delta) % len(rows)
        self._refresh_rows()

    def enter(self) -> None:
        if self._pending is not None:
            self.confirm_pending()
            return
        if self._picker is not None:
            self._pick()
            return
        if self._form is not None:
            if self._form.enter():
                return                      # Enter израсходован списком формы
            self._submit_form()
            return
        if self._legacy_open:
            if self._on_legacy_save is not None:
                self._on_legacy_save()
            return
        row = self._row_at_cursor()
        if row is None:
            return
        if row["kind"] == "reserve":
            self._open_reserve()
        elif row["kind"] in ("slot", "assign"):
            self._open_value_picker(row)
        else:
            self._open_form(row["alias"])

    def escape(self) -> bool:
        """Свернуть СВОЁ под-состояние и съесть клавишу; сворачивать нечего — False,
        и Esc достаётся экрану (тот уводит в меню). Вложенность из Global Constraints."""
        if self._pending is not None:
            self.cancel_pending()
            return True
        if self._picker is not None:
            self._close_picker()
            return True
        if self._form is not None:
            if self._form.escape():
                return True                 # закрыт только список формы
            self._form = None
            self._legacy_open = self._legacy_default()  # назад в СВОЁ состояние, не в меню
            self._rebuild()
            return True
        if self._legacy_open and not self.registry_empty:
            self._legacy_open = False       # под-форма резервной — назад к списку
            self._rebuild()
            return True
        return False

    def new_entry(self) -> bool:
        """`n`/`Alt+N`: новая запись реестра. False — состояние занято, клавиша не наша.

        Занято — это форма, пикер, confirm И под-форма резервной модели: в последней
        лежит несохранённый выбор, а её футер `Alt+N` не обещает. Пустое состояние
        исключение по построению: там вход в реестр и есть смысл клавиши, а «терять»
        нечего — легаси-редактор поднимается из файла заново."""
        if self._form is not None or self._picker is not None or self._pending is not None:
            return False
        if self._legacy_open and not self.registry_empty:
            return False
        self._open_form(None)
        return True

    def alt_down(self) -> bool:
        if self._form is not None:
            return self._form.reload_models() or self._form.reload_presets()
        if self._legacy_open and self._legacy is not None:
            return bool(self._legacy.reload())
        if self._picker is not None:
            return False        # список уже раскрыт, а перечитывать в нём нечего
        row = self._row_at_cursor()
        if row is not None and row["kind"] in ("slot", "assign"):
            # Канон клавиш: `Alt+↓` — раскрыть список. Список у строки тот же, что и
            # у Enter (решение 9 называет обе клавиши), а не вторая механика.
            self._open_value_picker(row)
            return True
        return False

    def probe(self) -> bool:
        """`Ctrl+Space` — проба соединения; она есть только у формы записи (у списка
        и у легаси-редактора пробы нет, §5.2 спеки клавиш)."""
        if self._form is not None:
            return self._form.probe_connection()
        return False

    def space(self) -> bool:
        if self._form is not None:
            return self._form.toggle_no_auth()
        return False

    # --- под-формы ------------------------------------------------------------
    def _open_form(self, alias: str | None) -> None:
        from tui.editors.model_entry_form import ModelEntryForm
        initial = None
        if alias is not None:
            entry = self._entries.get(alias) or {}
            initial = {"alias": alias, "model": entry.get("model"),
                       "url": entry.get("url"), "token": entry.get("token")}
        self._form = ModelEntryForm(
            initial=initial, existing_aliases=self._aliases, presets=self._presets,
            effective_url=self._effective_url, effective_auth=self._effective_auth,
            verify=self._verify, config_proxy=self._config_proxy)
        self._legacy_open = False           # форма занимает тело целиком
        self._rebuild()

    def _open_reserve(self) -> None:
        self._legacy_open = True
        self._rebuild()

    # --- запись ---------------------------------------------------------------
    def _submit_form(self) -> None:
        ok, alias, fields = self._form.submit()
        if not ok:
            return                          # ошибка осталась В форме, писать нечего
        ops = [("upsert_model", alias, fields)]
        if self._needs_default_slot(alias):
            # Вторая модель делает реестр неоднозначным (спека §4): валидатор отвергнет
            # такую запись отдельной операцией — и правильно. Спрашиваем слот и уходим
            # ОДНОЙ транзакцией (решение Task 1).
            aliases = sorted({*self._aliases, alias})
            self._open_picker(aliases, lambda chosen: self._write(
                ops + [("write_slot", "default", chosen)], alias))
            return
        self._write(ops, alias)

    def _needs_default_slot(self, alias: str) -> bool:
        after = {*self._aliases, alias}
        return len(after) >= 2 and not any(self._slots.get(level) for level in LEVELS)

    def _write(self, ops: list, alias: str) -> None:
        from config_modules.registry_write import RegistryWriteError, apply_registry_ops
        try:
            apply_registry_ops(self._target, ops, self._template,
                               is_profile=self._is_profile)
        except (RegistryWriteError, OSError) as exc:
            self._notice(f"✗ не сохранено: {exc}", ok=False)
            return
        self._form = None
        self._close_picker()
        self._reload()
        self._cursor_to(alias)
        self._refresh_rows()
        self._changed()
        if not self._entry_address(alias):
            self._notice(f"⚠ запись «{alias}» сохранена в {Path(self._target).name}, "
                         f"но {_NO_ADDRESS_HINT}", ok=None)
            return
        self._notice(f"Запись «{alias}» сохранена — {Path(self._target).name}", ok=True)

    def _cursor_to(self, alias: str) -> None:
        if alias in self._aliases:
            self._idx = self._aliases.index(alias)

    def _write_binding(self, op: tuple, done: str) -> None:
        """Запись слота/привязки — немедленно по выбору, через тот же слой с
        валидацией результата (решение 2): нерезолвимая комбинация (снятый последний
        слот при двух моделях в реестре) отвергается целиком, файл байт-в-байт не
        тронут, отказ называет причину от валидатора."""
        from config_modules.registry_write import RegistryWriteError, apply_registry_ops
        self._close_picker()
        try:
            apply_registry_ops(self._target, [op], self._template,
                               is_profile=self._is_profile)
        except (RegistryWriteError, OSError) as exc:
            self._notice(f"✗ не сохранено: {exc}", ok=False)
            return
        self._reload()
        self._refresh_rows()        # подводка курсора — как у записи в реестр
        self._changed()
        self._notice(f"{done} — {Path(self._target).name}", ok=True)

    @staticmethod
    def _slot_notice(level: str, chosen) -> str:
        if chosen is None:
            return f"Слот «{level}» снят"
        return f"Уровень «{level}» → «{chosen}»"

    @staticmethod
    def _assign_notice(consumer: str, chosen) -> str:
        label = consumer_labels()[consumer]
        if chosen is None:
            return f"Привязка «{label}» снята"
        return f"Привязка «{label}» → «{chosen}»"

    # --- удаление -------------------------------------------------------------
    def request_remove(self) -> bool:
        if self._pending is not None or self._form is not None or self._picker is not None:
            return False
        row = self._row_at_cursor()
        if row is None or row["kind"] != "entry":
            return False                    # резервную модель удалять нечем — она поле
        from config_modules.registry_write import RegistryWriteError, references_to
        alias = row["alias"]
        try:
            references = references_to(self._target, alias)
        except (RegistryWriteError, OSError) as exc:
            # Файл испортили под ногами между кадрами: спрашивать «удалить?» не о чем,
            # но и падать нельзя — говорим причину.
            self._notice(f"✗ не удалено: {exc}", ok=False)
            return False
        last = len(self._aliases) == 1
        self._pending = {"alias": alias, "references": references, "last": last}
        self.mount(Static(Text(self._confirm_text(alias, references, last), style=WARN),
                          classes="models-confirm onb-dyn", markup=False))
        return True

    @staticmethod
    def _confirm_text(alias: str, references: list, last: bool) -> str:
        parts = [f"Удалить запись «{alias}» из реестра?"]
        if references:
            parts.append("На неё ссылаются " + ", ".join(references)
                         + " — ссылки снимутся вместе с ней.")
        if last:
            parts.append("Реестр станет пустым — уровни выключатся.")
        parts.append("Enter — удалить · Esc — отмена")
        return " ".join(parts)

    def confirm_pending(self) -> None:
        pending = self._pending
        self._clear_confirm()
        if pending is None:
            return
        from config_modules.registry_write import RegistryWriteError, remove_model
        try:
            remove_model(self._target, pending["alias"], force=True,
                         is_profile=self._is_profile)
        except (RegistryWriteError, OSError) as exc:
            self._notice(f"✗ не удалено: {exc}", ok=False)
            return
        self._reload()
        self._changed()
        self._notice(f"Запись «{pending['alias']}» удалена — {Path(self._target).name}",
                     ok=True)

    def cancel_pending(self) -> None:
        self._clear_confirm()

    def _clear_confirm(self) -> None:
        self._pending = None
        for widget in self.query(".models-confirm"):
            widget.remove()

    # --- инлайн-пикер значений (решение 9: свой механизм раскрытия) -----------
    def _row_widget(self, idx: int):
        widgets = list(self.query(".models-row"))
        return widgets[idx] if 0 <= idx < len(widgets) else None

    def _open_value_picker(self, row: dict) -> None:
        """Список значений строки слота/привязки — ПОД ней (решение 9).

        Слот указывает на алиас реестра, привязка — ещё и на уровень (индирекция
        через слоты). Первая позиция обеих — снятие значения: отдельной клавиши
        снятия нет (Global Constraints), а «не задан»/«активная» — не значения, а
        отсутствие ключа в файле. `on_pick` получает `None` именно за неё —
        сентинел опознаётся ПОЗИЦИЕЙ и до записи доезжает тем же `None`, которым
        снимает ключ слой записи."""
        anchor = self._row_widget(self._idx)
        if row["kind"] == "slot":
            level = row["level"]
            self._open_picker(
                self._aliases,
                lambda chosen: self._write_binding(
                    ("write_slot", level, chosen), self._slot_notice(level, chosen)),
                hint=_PICKER_SLOT_HINT, unset_label=_SLOT_UNSET, after=anchor)
            return
        consumer = row["consumer"]
        self._open_picker(
            [*LEVELS, *self._aliases],
            lambda chosen: self._write_binding(
                ("write_assign", consumer, chosen),
                self._assign_notice(consumer, chosen)),
            hint=_PICKER_ASSIGN_HINT, unset_label=_ASSIGN_ACTIVE, after=anchor)

    def _open_picker(self, items: list, on_pick, *, hint: str = _PICKER_DEFAULT_HINT,
                     unset_label: str | None = None, after=None) -> None:
        """`unset_label` — подпись позиции «снять значение»: список получает её ПЕРВОЙ
        строкой сам, и вызывающему нечего перепутать. Списку без снятия (вопрос «какая
        модель по умолчанию» — там снятие невозможно, слот обязателен) она не нужна."""
        rows = ([unset_label] if unset_label is not None else []) + list(items)
        self._picker = {"items": rows, "idx": 0, "on_pick": on_pick,
                        "unset": unset_label is not None}
        scroll = VerticalScroll(classes="dropdown-scroll onb-dyn models-picker")
        scroll.can_focus = False
        self.mount(scroll, after=after)
        if not scroll.is_attached:
            return
        scroll.mount(*[Static("", classes="onb-option onb-dyn models-picker-row")
                       for _ in items])
        self.mount(Static(hint, classes="onb-hint onb-dyn models-picker-hint",
                          markup=False), after=scroll)
        self._refresh_picker_rows()

    def _refresh_picker_rows(self) -> None:
        if self._picker is None:
            return
        widgets = list(self.query(".models-picker-row"))
        for i, (widget, value) in enumerate(zip(widgets, self._picker["items"])):
            unset = self._picker["unset"] and i == 0     # снятие — позиция, не текст
            text = Text()
            text.append("▸ " if i == self._picker["idx"] else "  ", style=ACCENT)
            text.append(value, style=DEEP_DIM if unset else value_style(value))
            widget.update(text)
            widget.set_class(i == self._picker["idx"], "selected")
        if 0 <= self._picker["idx"] < len(widgets):
            # Своя строка в раскрытом списке — тот же приём, что у `field_list`:
            # ↑/↓ крутят ВНУТРЕННИЙ скролл `.dropdown-scroll`, за курсором Textual
            # не следит.
            self.call_after_refresh(widgets[self._picker["idx"]].scroll_visible,
                                    animate=False)

    def _pick(self) -> None:
        picker = self._picker
        if picker is None:
            return
        idx = picker["idx"]
        # Первая позиция списка со снятием — «значения нет», и наружу уходит `None`,
        # а не её подпись: сравнивать выбранное с текстом «не задан»/«активная»
        # нельзя — алиас с таким именем схемой разрешён.
        value = None if picker["unset"] and idx == 0 else picker["items"][idx]
        picker["on_pick"](value)

    def _close_picker(self) -> None:
        self._picker = None
        for widget in self.query(".models-picker, .models-picker-hint"):
            widget.remove()

    # --- связь с экраном ------------------------------------------------------
    def _notice(self, text: str, *, ok) -> None:
        if self._on_notice is not None:
            self._on_notice(text, ok=ok)

    def _changed(self) -> None:
        if self._on_change is not None:
            self._on_change()
