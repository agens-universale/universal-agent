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
from tui.form_frame import KEEP_IN_VIEW, reveal_focus
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
# «Ctrl+U — очистить» — обещание встроенного биндинга Textual Input
# (delete_left_all, конвенция readline): он работает во всех полях, но обещан на
# адресе — там очистка значит «вернуть наследование», и владелец её не находил.
_FOOTER_FORM_URL = ("Enter — сохранить · Alt+↓ — показать список · "
                    "Ctrl+U — очистить · Ctrl+Space — проверить связь · "
                    "Esc — к списку")
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
# Ячейка адреса мёртвой записи: причина «мёртвости» видна в самой строке, длинное
# объяснение (`_NO_ADDRESS_HINT`) остаётся в notice сохранения.
_DEAD_ADDRESS_CELL = "без адреса"
# Строка-действие в конце блока «реестр»: Enter/клик = клавиша n. DIM + многоточие
# (пробы владельца: DEEP_DIM читался подсказкой, тил перетягивал глаз с данных;
# DIM — виден сразу, но не спорит с реестром и цветами уровней).
_ADD_ROW_LABEL = "+ добавить модель…"
_PICKER_DEFAULT_HINT = ("в реестре станет больше одной модели — выберите, какая "
                        "работает по умолчанию")
# Хвост записи называет ВСЕ уровни, которыми она работает, а не один `default`:
# «одна модель на двух уровнях» — обычная раскладка (light+max на быстрой), и
# показывать из неё половину значит врать про вторую.
_LEVEL_TAIL_LEAD = "←"
_LEVEL_TAIL_SEP = ", "
# Хвост ячейки адреса у записи без своего `url`: адрес ей достаётся от «Подключения»,
# и это факт про запись, а не про колонку. Режется он первым (см. `_entry_row_text`) —
# хост переживает усечение, происхождение адреса можно дочитать в подсказке строки.
_INHERITED_TAIL = " · из основного конфига"
_ELLIPSIS = "…"
# Уже колонки адрес перестаёт быть адресом («o…») — такую колонку честнее убрать
# целиком и отдать место остальным.
_MIN_ADDRESS_COLS = 4
# Голова строки слота/привязки — «имя · описание роли» (грамматика `_mode_row_text`).
# Под узкую рамку жертвуют описанием: уже `_MIN_ROLE_COLS` оно перестаёт быть
# описанием и уходит целиком вместе с разделителем. Имя — ядро строки, режется
# последним. `_MIN_HEAD_COLS` — предел и для него.
_HEAD_SEP = " · "
_MIN_ROLE_COLS = 4
_MIN_HEAD_COLS = 4

# Подписи блоков секции, вписанные в черту-разделитель. Заголовка «Модели» внутри
# рамки нет: его слово уже стоит в шапке экрана («Настройки · Модели»), а строк на
# экране и без повтора много.
_BLOCK_CAPTIONS = {
    "entry": "реестр",
    "slot": "уровни",
    "assign": "назначения: кто чем работает",
}
_BLOCK_TAIL_MIN = 4
# Все черты-подписи ОДНОЙ ширины — по самой длинной подписи плюс минимальный
# хвост (ревизия владельца: разная длина линий читалась как неровность, а не
# как форма). Новая длинная подпись раздвинет все черты сама. Слева вместо
# черты — стрелка сворачивания (▾/▸): шапка блока стала его выключателем.
_BLOCK_CAPTION_WIDTH = max(
    len(f"▾ {text} ") for text in _BLOCK_CAPTIONS.values()
) + _BLOCK_TAIL_MIN
# Порядок блоков секции — он же порядок обхода в `_rows`/`_body`.
_BLOCK_ORDER = ("entry", "slot", "assign")

# Сворачивание блоков живёт на процесс (решение владельца: память на сессию,
# в файлы не пишется), ключ — файл цели: основной конфиг и каждый профиль
# помнят своё, а пересборка/перемонтирование секции состояние не теряет.
_COLLAPSED_BLOCKS: dict = {}


class _BlockHeader(Static):
    """Шапка блока — единственный МЫШИНЫЙ элемент секции: сворачивание только
    кликом, без клавиш (решение владельца). Это безопасно ровно потому, что
    дефолт — «всё раскрыто»: без мыши пользователь ничего не теряет, фича
    аддитивна. Позицией курсора шапка не является — путь стрелок не растёт."""

    def __init__(self, text, *, kind: str, on_toggle, **kwargs) -> None:
        super().__init__(text, **kwargs)
        self._kind = kind
        self._on_toggle = on_toggle

    def on_click(self, event) -> None:
        event.stop()
        self._on_toggle(self._kind)


def fit_cell(text: str, width: int) -> str:
    """Содержимое колонки под её ширину: не влезло — многоточие в хвосте (канон
    усечения базы; целыми словами здесь резать нечего — хвост ячейки адреса и есть
    то, чем жертвуют, а хост стоит в начале и переживает срез)."""
    if width <= 0 or len(text) <= width:
        return text
    return _ELLIPSIS if width == 1 else text[:width - 1] + _ELLIPSIS


def fit_head(head: str, width: int) -> str:
    """Голова строки слота/привязки под ширину колонки — та же лестница жертв
    all-or-nothing, что у колонок строки реестра, только сверху вниз по одной строке:

    1. описание роли ужимается многоточием, пока в нём остаётся ≥ `_MIN_ROLE_COLS`;
    2. не осталось — описание уходит ЦЕЛИКОМ вместе с разделителем «·» (огрызок
       «о…» описанием уже не является, а место занимает);
    3. не влезает и имя — ужимается оно: строка не имеет права переноситься ни на
       какой ширине, но имя уровня/потребителя видно до последнего.

    У служебных потребителей («суммаризация», «судья finish_gate») разделителя нет —
    вся голова считается именем и режется только на третьей ступени."""
    if width <= 0 or len(head) <= width:
        return head
    name, sep, role = head.partition(_HEAD_SEP)
    if sep and width >= len(name) + len(sep) + _MIN_ROLE_COLS:
        return name + sep + fit_cell(role, width - len(name) - len(sep))
    return fit_cell(name, width)


def address_host(url: str) -> str:
    """Хост адреса — то, чем записи РАЗЛИЧАЮТСЯ: путь у них один и тот же
    (`/v1/chat/completions`), и колонку он бы только съел. Полный адрес остаётся в
    подсказке строки."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    return parsed.netloc or parsed.path.split("/")[0] or url

# --- нижняя половина секции: слоты (уровень → алиас) и привязки потребителей ---
_SLOT_UNSET = "не задан"
_ASSIGN_ACTIVE = "активная"
# Правая колонка строки «активная» в пикере: снятие привязки не фиксирует ничего —
# потребитель следует за активным уровнем (`ModelRouter.resolve`, ветка без binding).
# «сейчас <уровень>» — вычисленное на этот момент, слово «сейчас» и помечает его
# как текущее состояние, а не как часть выбора.
_ACTIVE_DESC_LEAD = "работает активным уровнем: сейчас "
_SLOT_DEAD_HINT = "уровень не резолвится: задайте хотя бы один слот"
# Подписи потребителей, которых НЕТ в транспортном словаре `_CONSUMER_RU_LABEL`
# (`utils/llm_chat_client.py`): тот заведён для notice деградации и остаётся его
# собственностью — здесь только UI-добавка (контракт п.2 волны B).
#
# `auto_judge` — потребитель волны C: строка в реестре есть, но никто его ещё не
# зовёт, а без привязки он пойдёт уровнем default, не активным (`ModelRouter.
# resolve`). Описание «появится с авто-выбором» — в голове строки грамматикой
# «имя · описание» (построчной подсказки у секции больше нет — строка объясняет
# себя сама).
#
# `*` — не «те, кого нет в списке»: в списке перечислены ВСЕ канонические имена, и
# поимённо чужое имя вписать нельзя (валидатор фазы 2 отвергает ключ). Реальный
# смысл ровно два: единственный способ привязать кастомных субагентов из
# `.md`-файлов и общая привязка «всем сразу, кому своя не задана»
# (`ModelRouter._lookup_binding`: точное имя → `*`).
_UI_CONSUMER_LABEL = {
    "auto_judge": "судья auto_judge · появится с авто-выбором",
    "*": "* · кастомные и будущие субагенты",
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


def slot_head_text(level: str, width: int = 0) -> Text:
    """Голова строки слота: имя уровня — ЦВЕТОМ уровня (тот же `TOGGLES["level"]`, что
    красит уровни в раскрытом списке: одна сущность не имеет права выглядеть на одном
    экране двумя способами), роль после «·» — обычным INK.

    Текст берётся у `slot_head` целиком, а не собирается заново: ширину колонки
    считают по нему же, и вторая сборка развела бы витрину с сеткой. Под колонку он
    подгоняется ДО раскраски (`width`, 0 — не ограничивать): красить обрезки уже
    готового `Text` значило бы держать правило цвета в двух местах. Срез имени цвет
    переживает — режется хвост, а не начало."""
    head = fit_head(slot_head(level), width)
    t = Text()
    t.append(head[:len(level)], style=value_style(level))
    t.append(head[len(level):], style=INK)
    return t


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
                 active_level: str = "default",
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
        # Активный уровень — знание ЭКРАНА (config + сессионные переключатели);
        # секция показывает файл, но описание «активной» в пикере говорит о рантайме.
        self._active_level = active_level
        self._legacy_editor_factory = legacy_editor_factory
        self._legacy_hints = list(legacy_hints)
        self._on_change = on_change
        self._on_notice = on_notice
        self._on_legacy_save = on_legacy_save
        self._document = self._read_document()
        # Свёрнутые блоки — общий на процесс словарь по файлу цели (см. коммент
        # у `_COLLAPSED_BLOCKS`): set видов из `_BLOCK_ORDER`.
        self._collapsed = _COLLAPSED_BLOCKS.setdefault(str(target), set())
        self._idx = 0                     # курсор строки списка (записи + резервная)
        self._form = None                 # ModelEntryForm в работе
        # Легаси-редактор — СОСТОЯНИЕ (флаг), а виджет пересоздаётся каждой сборкой
        # тела: снесённый Textual'ом виджет обратно не монтируется, и переиспользование
        # ссылки давало пустое тело секции.
        self._legacy_open = self._legacy_default()
        self._legacy = None
        self._measured_width = None       # ширина рамки, под которую посчитаны колонки
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
        rows: list = []
        if "entry" not in self._collapsed:
            rows += [{"kind": "entry", "alias": a} for a in self._aliases]
            # «+ добавить» — последней строкой БЛОКА (ревизия владельца): курсор
            # при входе встаёт на данные, действие — «…и ещё одна» в конце.
            # Клавиша n остаётся путём из любого места секции.
            rows.append({"kind": "add"})
        if "slot" not in self._collapsed:
            rows += [{"kind": "slot", "level": level} for level in LEVELS]
        if "assign" not in self._collapsed:
            rows += [{"kind": "assign", "consumer": c} for c in assign_consumers()]
        if not self._is_profile:
            rows.append({"kind": "reserve"})
        return rows

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

    def _model_width(self) -> int:
        """Колонка провайдерского id — по записям реестра. Строка «резервная модель»
        её не расширяет: её значение — последнее в строке, за ним только собственный
        хинт, и выравнивать там нечего."""
        return max([len(self._entry_model(a)) for a in self._aliases] + [0])

    def _entry_model(self, alias: str) -> str:
        entry = self._entries.get(alias)
        entry = entry if isinstance(entry, dict) else {}
        return str(entry.get("model") or alias)

    def _address_cell(self, alias: str) -> str:
        """Ячейка адреса: хост, куда реально уйдёт запрос, плюс происхождение адреса,
        если он унаследован. Адрес не резолвится вовсе — ячейка называет это сама
        («без адреса»): построчной подсказки у секции больше нет, а «по этой записи
        запрос не уйдёт» — факт про запись, и место ему в её строке."""
        address = self._entry_address(alias)
        if not address:
            return _DEAD_ADDRESS_CELL
        own = (self._entries.get(alias) or {}).get("url")
        return address_host(address) + ("" if own else _INHERITED_TAIL)

    def _entry_levels(self, alias: str) -> list:
        """Уровни, которые указывают на эту запись, — в каноническом порядке `LEVELS`,
        а не в порядке ключей файла: колонка читается как трек уровней."""
        return [level for level in LEVELS if self._slots.get(level) == alias]

    def _pointer_text(self, alias: str, *, dead: bool = False) -> Text:
        """Указатель уровней: `← light, max`. Каждое слово — цветом своего уровня
        (§12.4, тот же источник, что у колонки значения), стрелка и запятые — DIM.
        У мёртвой записи гаснет и он: цвет уровня здесь означает «работает», а такая
        запись не работает (слот на неё смотрит, но адреса у неё нет)."""
        levels = self._entry_levels(alias)
        t = Text()
        if not levels:
            return t
        t.append(_LEVEL_TAIL_LEAD + " ", style=DEEP_DIM if dead else DIM)
        for i, level in enumerate(levels):
            if i:
                t.append(_LEVEL_TAIL_SEP, style=DEEP_DIM if dead else DIM)
            t.append(level, style=DEEP_DIM if dead else value_style(level))
        return t

    def _address_width(self) -> int:
        """Натуральная ширина колонки адреса — по самой длинной ячейке. Колонка НЕ
        растягивается на весь остаток рамки: иначе указатель уезжал бы к правому краю
        и переставал читаться как столбец, стоящий в общей сетке."""
        return max([len(self._address_cell(a)) for a in self._aliases] + [0])

    def _pointer_width(self) -> int:
        """Ширина столбца указателя — по самому длинному в списке: все указатели стоят
        друг под другом, сразу за колонкой адреса.

        Усечения у него нет — «← light, default» пополам не режут: на узкой рамке он
        уходит целиком, третьей ступенью лестницы `_entry_columns` (второй жертвой
        после адреса). Ширина, при которой это происходит, — в докстринге лестницы,
        здесь только сам замер."""
        return max([len(self._pointer_text(a).plain) for a in self._aliases] + [0])

    def _row_width(self) -> int:
        """Ширина строки внутри рамки (padding 0 1 — минус 2) — carry-fix того же рода,
        что `_modes_row_width` экрана: региона нет до layout-прохода, 0 читается как
        «не измерено» и усечения не даёт. Последний матч — свежесмонтированная строка."""
        widgets = list(self.query(".models-row"))
        if not widgets:
            return 0
        return max(0, widgets[-1].region.width - 2)

    def _entry_columns(self) -> tuple:
        """Раскладка колонок строки записи под ТЕКУЩУЮ ширину рамки:
        `(alias_w, model_w, address_w, pointer_w)`.

        Порядок жертв — канон all-or-nothing `_mode_row_text` («не влезло — хвост
        уходит целиком, ядро строки видимо всегда»), только ступеней здесь четыре:

        1. колонка адреса стоит по своему содержимому (`_address_width`) и ужимается
           многоточием только когда рамке не хватает, до `_MIN_ADDRESS_COLS`;
        2. не осталось и этого — колонка адреса уходит ЦЕЛИКОМ вместе со своим
           зазором (иначе строка длиннее рамки: Static её перенесёт, а перенос рвёт
           указатель пополам и ставит хвост к левому краю без отступа);
        3. не хватает и на указатель — уходит и он;
        4. ядро (алиас + id) шире рамки — ужимается id, в пределе и алиас: строка не
           имеет права переноситься НИ на какой ширине.

        `address_w is None` — рамка ещё не измерена (первый кадр): усечения нет,
        досчитает carry-fix `_refresh_rows`. `0` — колонки нет.

        Раскладка ОДНА на список, а не своя у каждой строки: колонки стоят друг под
        другом, и посчитанная построчно она бы их развалила."""
        alias_w, model_w = self._alias_width(), self._model_width()
        pointer_w = self._pointer_width()
        width = self._row_width()
        if width <= 0:
            return alias_w, model_w, None, pointer_w
        core = 2 + alias_w + (2 + model_w if model_w else 0)
        tail = (2 + pointer_w) if pointer_w else 0
        address_w = min(width - core - 2 - tail, self._address_width())
        if address_w >= _MIN_ADDRESS_COLS:
            return alias_w, model_w, address_w, pointer_w
        if core + tail <= width:
            return alias_w, model_w, 0, pointer_w
        if core <= width:
            return alias_w, model_w, 0, 0
        model_w = max(0, width - 2 - alias_w - 2)
        if model_w:
            return alias_w, model_w, 0, 0
        return max(0, width - 2), 0, 0, 0

    def _entry_row_text(self, alias: str, selected: bool) -> Text:
        """Строка записи: алиас · провайдерский id · адрес · указатель уровней.
        Иерархия колонок читается цветом (INK → DIM → DEEP_DIM): чем правее, тем
        служебнее. Мёртвая запись (адрес не резолвится) гаснет ЦЕЛИКОМ — вместе с
        указателем: «работает уровнем max» и «по этой записи запрос не уйдёт» в одной
        строке спорили бы друг с другом.

        Колонок, не поместившихся в рамку, в строке просто нет (см. `_entry_columns`)
        — каждая приписывается вместе со СВОИМ зазором."""
        dead = not self._entry_address(alias)
        alias_w, model_w, address_w, pointer_w = self._entry_columns()
        t = Text()
        t.append("▸ " if selected else "  ", style=ACCENT)
        t.append(fit_cell(alias, alias_w).ljust(alias_w),
                 style=DEEP_DIM if dead else INK)
        if model_w:
            t.append("  ")
            t.append(fit_cell(self._entry_model(alias), model_w).ljust(model_w),
                     style=DEEP_DIM if dead else DIM)
        if address_w is None or address_w:
            cell = self._address_cell(alias)
            t.append("  ")
            t.append(cell if address_w is None else fit_cell(cell, address_w).ljust(address_w),
                     style=DEEP_DIM)
        if pointer_w:
            pointer = self._pointer_text(alias, dead=dead)
            if pointer.plain:
                t.append("  ")
                t.append_text(pointer)
        return t

    def _reserve_row_text(self, selected: bool) -> Text:
        """Строка резервной модели — DEEP_DIM ЦЕЛИКОМ: тем же приёмом «есть, но сейчас
        не работает» помечены нерабочие записи реестра. В списке она появляется только
        рядом с непустым реестром (`_rows`), а при пустом её место занимает сам
        легаси-редактор — значит равноправной она не бывает никогда.

        Колонка подписи — ТА ЖЕ, что колонка алиасов реестра (`_entry_columns`), а не
        своя по `_alias_width`: строка относится к списку над ней и обязана стоять в
        его сетке — в том числе когда лестница реестра эту колонку ужимает. Двойного
        учёта нет: подпись подпирает колонку снизу ВНУТРИ `_alias_width`, и отсюда
        берётся ровно та же величина.

        Ширина — канон all-or-nothing `_mode_row_text`: хвост-подсказка уходит ЦЕЛИКОМ
        (дословно та же фраза стоит в подсказке под курсором — терять нечего), дальше
        ужимается сама модель, в пределе остаётся одна подпись."""
        label_w = self._entry_columns()[0]
        width = self._row_width()
        model = self._reserve_model() or _RESERVE_UNSET
        t = Text()
        t.append("▸ " if selected else "  ", style=ACCENT)
        if label_w:
            t.append(fit_cell(_RESERVE_LABEL, label_w).ljust(label_w), style=DEEP_DIM)
        model_w = len(model) if width <= 0 else max(0, width - 2 - label_w - 2)
        if model_w:
            t.append("  ")
            t.append(fit_cell(model, model_w), style=DEEP_DIM)
        tail = "  " + _RESERVE_HINT
        if width <= 0 or len(t.plain) + len(tail) <= width:
            t.append(tail, style=DEEP_DIM)
        return t

    def _binding_columns(self) -> tuple:
        """(head_w, value_w) — колонки нижней половины секции под ТЕКУЩУЮ ширину
        рамки. Сетка у слотов и привязок ОДНА (значения стоят друг под другом сквозь
        оба блока: вопрос у них общий — «чем работает»), формулы — канон
        `_modes_columns` (`settings.py`): +3 воздуха к самой длинной голове, +2 зазора
        перед значением, ширина значения по макс. длине ВСЕХ имён, которые строка
        может показать.

        Под узкой рамкой — та же лестница жертв all-or-nothing, что у строки реестра
        (`_entry_columns`), тремя ступенями:

        1. `head_w` ужимается — сперва за счёт воздуха, потом самих голов (описание
           роли режется первым, см. `fit_head`), пока в колонке ≥ `_MIN_HEAD_COLS`;
        2. не осталось и этого — ужимается колонка ЗНАЧЕНИЯ, а голове остаётся
           минимум: без имени строка перестаёт быть строкой;
        3. не влезает и это — колонка значения уходит целиком вместе со своим
           зазором, голова занимает всю рамку.

        Раскладка ОДНА на список: колонки стоят друг под другом, посчитанная построчно
        она бы их развалила. Ширина 0 (рамка ещё не измерена) — колонки натуральные,
        досчитает carry-fix `_refresh_rows`."""
        labels = consumer_labels()
        heads = [slot_head(level) for level in LEVELS]
        heads += [labels[consumer] for consumer in assign_consumers()]
        values = ([self._slot_unset_label(lvl) for lvl in LEVELS
                   if not self._slot_value(lvl)]
                  + [_ASSIGN_ACTIVE, *LEVELS, *self._aliases])
        head_w = max(len(h) for h in heads) + 3
        value_w = max(len(v) for v in values)
        width = self._row_width()
        if width <= 0:
            return head_w, value_w
        head_w = min(head_w, width - 2 - 2 - value_w)
        if head_w >= _MIN_HEAD_COLS:
            return head_w, value_w
        value_w = width - 2 - 2 - _MIN_HEAD_COLS
        if value_w > 0:
            return _MIN_HEAD_COLS, value_w
        return max(0, width - 2), 0

    def _binding_row_text(self, head_for, value, unset_label: str,
                          selected: bool) -> Text:
        """Строка слота/привязки — грамматика `_mode_row_text`: курсор, голова
        выровненным блоком, колонка значения своим цветом (§12.4). Трека здесь
        нет — список значений произвольный (алиасы реестра), фиксированных позиций
        под него не существует.

        Голову строит ВЫЗЫВАЮЩИЙ (`head_for(head_w) -> Text`): у слота её первое слово
        несёт цвет уровня, у привязки вся голова обычная — раскраску решает тот, кто
        знает, ЧТО за строка, а ширину колонки знает только сетка, и она же её отдаёт.

        Колонки значения может не быть вовсе (последняя ступень лестницы) — тогда нет
        и её зазора, ровно как у колонок строки реестра.

        `value is None` — ключа в файле нет: подпись `unset_label` и DEEP_DIM по
        ФАКТУ отсутствия, а не по совпадению значения с этой подписью."""
        head_w, value_w = self._binding_columns()
        shown = unset_label if value is None else value
        t = Text()
        t.append("▸ " if selected else "  ", style=ACCENT)
        if head_w:
            head = head_for(head_w)
            head.pad_right(max(0, head_w - len(head.plain)))
            t.append_text(head)
        if value_w:
            t.append("  ")
            t.append(fit_cell(shown, value_w).ljust(value_w),
                     style=DEEP_DIM if value is None else value_style(value))
        return t

    def _slot_unset_label(self, level: str) -> str:
        """Значение пустого слота называет фоллбэк само: «не задан → default» —
        построчной подсказки больше нет, а «что реально работает вместо» — главный
        факт этой строки. Уровень-кандидат или единственная модель — одинаково
        стрелкой; не резолвится ничего — честное «не задан»."""
        candidate, alias = self._slot_fallback(level)
        target = candidate or alias
        return f"{_SLOT_UNSET} → {target}" if target else _SLOT_UNSET

    def _slot_row_text(self, level: str, selected: bool) -> Text:
        return self._binding_row_text(
            lambda w: slot_head_text(level, w), self._slot_value(level),
            self._slot_unset_label(level), selected)

    def _assign_row_text(self, consumer: str, selected: bool) -> Text:
        label = consumer_labels()[consumer]
        return self._binding_row_text(
            lambda w: Text(fit_head(label, w), style=INK), self._assign_value(consumer),
            _ASSIGN_ACTIVE, selected)

    def _row_text(self, row: dict, selected: bool) -> Text:
        if row["kind"] == "reserve":
            return self._reserve_row_text(selected)
        if row["kind"] == "slot":
            return self._slot_row_text(row["level"], selected)
        if row["kind"] == "assign":
            return self._assign_row_text(row["consumer"], selected)
        if row["kind"] == "add":
            t = Text()
            t.append("▸ " if selected else "  ", style=ACCENT)
            # Та же дисциплина, что у всех строк: не переносится НИ на какой ширине
            # (нашлось свипом стража — голый Static на узкой рамке переносился).
            width = self._row_width()
            label = _ADD_ROW_LABEL if width <= 0 else fit_cell(_ADD_ROW_LABEL,
                                                               max(0, width - 2))
            t.append(label, style=DIM)
            return t
        return self._entry_row_text(row["alias"], selected)

    def _slot_fallback(self, level: str) -> tuple:
        """Что работает ВМЕСТО незаданного слота: обход `LEVEL_FALLBACK` (тот же,
        что у `ModelRouter._resolve_level`). `(уровень, алиас)` — фоллбэк по матрице;
        `(None, алиас)` — правило «единственная модель реестра»; `(None, None)` —
        уровень не резолвится. Один обход на обе витрины — текст подсказки и
        крашеное описание в пикере."""
        for candidate in LEVEL_FALLBACK[level][1:]:
            fallback = self._slot_value(candidate)
            if fallback:
                return candidate, fallback
        if len(self._aliases) == 1:
            return None, self._aliases[0]
        return None, None

    def _slot_fallback_text(self, level: str) -> str:
        candidate, alias = self._slot_fallback(level)
        if candidate:
            return f"действует {candidate} → {alias}"
        if alias:
            return f"действует единственная модель реестра → {alias}"
        return _SLOT_DEAD_HINT

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
        if self.registry_empty:
            # Пустой реестр профиля: легаси-редактора, который держал бы это место в
            # config.yml, здесь не бывает — своё пустое состояние с входом в реестр.
            # Условие — именно пустота РЕЕСТРА: пустой `_rows` бывает и у профиля со
            # всеми свёрнутыми блоками, там телу положены шапки.
            yield Static(_PROFILE_EMPTY_HINT, classes="set-hint set-dyn", markup=False)
            return
        rows = self._rows()
        # Цель записи — константа всей секции, а не факт строки: одной статичной
        # строкой НАД списком, ПОЛНЫМ путём (ревизия владельца: одно имя файла не
        # говорит, какой именно это конфиг, а строка всё равно свободна). Построчной
        # подсказки под списком нет — каждая строка объясняет себя сама (адрес и
        # «без адреса» в колонке, фоллбэк в значении слота, описания в головах);
        # детали — в пикере и форме по Enter.
        yield Static(f"запись: {self._target}",
                     classes="set-hint set-dyn models-target", markup=False)
        # Обход ПО БЛОКАМ, а не по строкам: шапка есть и у свёрнутого блока (строк
        # у него нет), и она же — его выключатель (клик, `_BlockHeader`). Индексы
        # строк идут сквозь блоки тем же порядком, что `_rows`. Плотность B: строки
        # блока впритык, воздух — margin-top шапки.
        i = 0
        for kind in _BLOCK_ORDER:
            yield _BlockHeader(self._block_header_text(kind), kind=kind,
                               on_toggle=self._toggle_block,
                               classes="set-group-sep models-block set-dyn")
            if kind in self._collapsed:
                continue
            while i < len(rows) and rows[i]["kind"] in (kind, "add"):
                cls = "models-row set-row set-dyn" + (" selected" if i == self._idx else "")
                yield Static(self._row_text(rows[i], i == self._idx), classes=cls,
                             markup=False)
                i += 1
        if i < len(rows) and rows[i]["kind"] == "reserve":
            # «Резервная модель» — не блок, а одна строка: голая черта-отбивка,
            # сворачивать нечего.
            yield Rule(classes="set-group-sep")
            cls = "models-row set-row set-dyn" + (" selected" if i == self._idx else "")
            yield Static(self._row_text(rows[i], i == self._idx), classes=cls,
                         markup=False)

    def _block_header_text(self, kind: str) -> Text:
        """Шапка блока: стрелка состояния + подпись. У раскрытого подпись вписана в
        черту (хвост — до общей ширины `_BLOCK_CAPTION_WIDTH`): черта отделяет блок
        от соседей сверху. У свёрнутого черты НЕТ вовсе (ревизия владельца) —
        отделять нечего, шапка сама себе строка, — зато есть превью содержимого."""
        collapsed = kind in self._collapsed
        t = Text()
        t.append("▸ " if collapsed else "▾ ", style=DEEP_DIM)
        t.append(_BLOCK_CAPTIONS[kind], style=DEEP_DIM)
        if collapsed:
            preview = self._block_preview(kind)
            if preview.plain:
                t.append(" · ", style=DEEP_DIM)
                t.append_text(preview)
            return t
        tail = max(_BLOCK_TAIL_MIN, _BLOCK_CAPTION_WIDTH - len(t.plain) - 1)
        t.append(" " + "─" * tail, style=DEEP_DIM)
        return t

    def _block_preview(self, kind: str) -> Text:
        """Одна строка о содержимом свёрнутого блока. Реестр — счёт и хосты; уровни
        — вся раскладка (имена цветом уровня, канон §12.4); привязки — счёт и
        сколько переопределено (нули не перечисляем: «все активная» честнее)."""
        if kind == "entry":
            from tui.editors.model import _plural_models
            n = len(self._aliases)
            hosts: list = []
            for alias in self._aliases:
                address = self._entry_address(alias)
                host = address_host(address) if address else None
                if host and host not in hosts:
                    hosts.append(host)
            t = Text(f"{n} {_plural_models(n)}", style=DEEP_DIM)
            if hosts:
                t.append(" · ", style=DEEP_DIM)
                t.append(hosts[0], style=DEEP_DIM)
                if len(hosts) > 1:
                    t.append(f" +{len(hosts) - 1}", style=DEEP_DIM)
            return t
        if kind == "slot":
            t = Text()
            for i, level in enumerate(LEVELS):
                if i:
                    t.append(" · ", style=DEEP_DIM)
                t.append(level, style=value_style(level))
                alias = self._slot_value(level)
                t.append("→", style=DEEP_DIM)
                t.append(alias or "—", style=DIM if alias else DEEP_DIM)
            return t
        total = len(assign_consumers())
        bound = sum(1 for c in assign_consumers() if self._assign_value(c) is not None)
        tail = f"переопределено {bound}" if bound else "все активная"
        return Text(f"{total} · {tail}", style=DEEP_DIM)

    def _toggle_block(self, kind: str) -> None:
        """Клик по шапке. Пока экран держит инлайн-состояние (форма, пикер,
        confirm) — клик не наш: сворачивание под вопросом уводило бы контекст
        из-под него. Курсор после пересборки — клампом `_rows`."""
        if self._form is not None or self._picker is not None or self._pending is not None:
            return
        if kind in self._collapsed:
            self._collapsed.discard(kind)
        else:
            self._collapsed.add(kind)
        self._idx = min(self._idx, max(0, len(self._rows()) - 1))
        self._rebuild()

    def _legacy_hint_widgets(self):
        for text, cls in self._legacy_hints:
            yield Static(text, classes=cls, markup=False)

    def _make_legacy_editor(self):
        """Легаси-редактор строит ЭКРАН (он знает про резолвнутые адрес/токен и про
        происхождение поля), а секция решает, когда он на сцене, — и просит новый
        экземпляр на каждую сборку тела."""
        return self._legacy_editor_factory()

    def on_mount(self) -> None:
        self._schedule_layout_pass()

    def on_resize(self) -> None:
        """Живой ресайз меняет рамку — значит и раскладку колонок. Потолок высоты за
        экраном (`cap_frame_height`), ширина колонок — за секцией: её знает только
        она."""
        self._refresh_rows()

    def _schedule_layout_pass(self) -> None:
        """Досчитать строки ПОСЛЕ layout-прохода: ширина колонок выводится из региона
        строки, а региона у только что смонтированного виджета ещё нет — первый кадр
        рисует колонки без усечения, второй досчитывает (тот же carry-fix, что
        `_refresh_modes_rows` экрана)."""
        self.call_after_refresh(self._refresh_rows)

    def _confirm_width(self) -> None:
        """Самоподтверждение замера: одного прохода после раскладки мало — регион
        строки уезжает ещё раз, когда список перерастает вьюпорт и появляется
        скроллбар (минус колонка). Замер повторяется, пока не совпадёт сам с собой;
        сходимость гарантирована тем, что высота строк от их ширины не зависит —
        строка теперь не переносится ни на какой ширине (`_entry_columns`)."""
        if self._measured_width is not None and self._row_width() != self._measured_width:
            self._refresh_rows()

    def _rebuild(self) -> None:
        """Пересобрать тело под текущее состояние. Снос + монтаж в один такт легален
        ровно потому, что состояния не делят id (форма — `mef-*`, легаси-редактор —
        `onb-model*`), а строки списка id не имеют вовсе: DuplicateIds ловит
        столкновение в ОДНОМ родителе (урок `_singleton` экрана)."""
        self.remove_children()
        for widget in self._body():
            self.mount(widget)
        self._schedule_layout_pass()

    def _refresh_rows(self) -> None:
        if self._form is not None or self._legacy_open:
            return          # на экране под-форма — строк списка нет и быть не должно
        rows = self._rows()
        widgets = list(self.query(".models-row"))
        if len(widgets) != len(rows):
            self._rebuild()
            return
        # Ширина, под которую посчитаны колонки этого прохода: `_confirm_width`
        # сверится с ней после раскладки и перерисует, если регион успел уехать.
        self._measured_width = self._row_width()
        self.call_after_refresh(self._confirm_width)
        for i, (widget, row) in enumerate(zip(widgets, rows)):
            widget.update(self._row_text(row, i == self._idx))
            widget.set_class(i == self._idx, "selected")
        # Подсвеченную строку подводим сами: фокуса у строк нет, за курсором Textual
        # не следит, а список секции длиннее экрана — записи, три слота и полтора
        # десятка привязок. Через общий резолвер, а не напрямую: висящий вопрос
        # (confirm-баннер) обязан перебить курсор, кто бы из них ни заказал подводку
        # последним (см. `form_frame.reveal_focus`).
        if 0 <= self._idx < len(widgets):
            reveal_focus(self)

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
            rows = self._picker["rows"]
            self._picker["idx"] = (self._picker["idx"] + delta) % len(rows)
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
        elif row["kind"] == "add":
            self._open_form(None)
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
        prev = self._form.original_alias
        if prev is not None and alias != prev:
            self._write(self._rename_ops(prev, alias, fields), alias)
            return
        ops = [("upsert_model", alias, fields)]
        if self._needs_default_slot(alias):
            # Вторая модель делает реестр неоднозначным (спека §4): валидатор отвергнет
            # такую запись отдельной операцией — и правильно. Спрашиваем слот и уходим
            # ОДНОЙ транзакцией (решение Task 1).
            aliases = sorted({*self._aliases, alias})
            model_w = max(len(self._entry_model(a)) for a in aliases)
            self._open_picker(
                [self._picker_alias_item(a, model_w) for a in aliases],
                lambda chosen: self._write(
                    ops + [("write_slot", "default", chosen)], alias),
                hint=_PICKER_DEFAULT_HINT)
            return
        self._write(ops, alias)

    def _rename_ops(self, prev: str, alias: str, fields: dict) -> list:
        """Переименование записи — ОДНА транзакция: новая запись с полным содержимым
        старой (merge правок формы здесь, а не у писателя: под новым именем записи
        ещё нет, и писательский merge стартовал бы с пустоты, теряя нетронутые
        ключи), переезд всех ссылок slots/assign, снятие старой записи. Порядок
        важен: к моменту `remove_model` на старое имя уже никто не ссылается.
        Валидация — одна, на итог; занятое имя отвергла форма."""
        merged = dict(self._entries.get(prev) or {})
        for key, value in fields.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        ops: list = [("upsert_model", alias, merged)]
        for level in LEVELS:
            if self._slot_value(level) == prev:
                ops.append(("write_slot", level, alias))
        for consumer in assign_consumers():
            if self._assign_value(consumer) == prev:
                ops.append(("write_assign", consumer, alias))
        ops.append(("remove_model", prev, True))
        return ops

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
        banner = Static(Text(self._confirm_text(alias, references, last), style=WARN),
                        classes=f"models-confirm onb-dyn {KEEP_IN_VIEW}", markup=False)
        self.mount(banner)
        # Вопрос монтируется в КОНЕЦ тела секции, а тело длиннее вьюпорта рамки: без
        # подводки футер честно менялся на «Enter — удалить», а самого вопроса на
        # экране не было — Enter приходилось жать вслепую.
        reveal_focus(self)
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

    def _picker_alias_item(self, alias: str, model_w: int) -> dict:
        """Позиция-модель: справа ТЕ ЖЕ колонки, что у строки реестра (id + адрес),
        — по одному id модели не различить (две записи на один id, разные endpoint'ы),
        а выбор вслепую и был проблемой плоского списка."""
        desc = Text()
        desc.append(self._entry_model(alias).ljust(model_w), style=DIM)
        cell = self._address_cell(alias)
        if cell:
            desc.append("  ")
            desc.append(cell, style=DEEP_DIM)
        return {"value": alias, "label": alias, "desc": desc}

    def _picker_fallback_desc(self, level: str) -> Text:
        """Крашеная версия `_slot_fallback_text` для строк пикера: имя уровня — своим
        цветом (§12.4: уровень выглядит одинаково на всём экране), алиас — DIM как
        прочие алиасы-значения. Ветки без имени уровня остаются ровным DEEP_DIM."""
        candidate, alias = self._slot_fallback(level)
        if candidate is None:
            return Text(self._slot_fallback_text(level), style=DEEP_DIM)
        t = Text("действует ", style=DEEP_DIM)
        t.append(candidate, style=value_style(candidate))
        t.append(" → ", style=DEEP_DIM)
        t.append(alias, style=DIM)
        return t

    def _picker_level_desc(self, level: str) -> Text:
        """Позиция-уровень отвечает «что реально произойдёт»: на какую модель уровень
        указывает СЕЙЧАС, — а не описание роли (оно стоит в блоке «уровни» выше)."""
        alias = self._slot_value(level)
        t = Text()
        if alias:
            # «→» — не `_LEVEL_TAIL_LEAD`: у указателя реестра стрелка смотрит
            # НА запись («← light»), здесь — ОТ уровня к модели.
            t.append("→ ", style=DEEP_DIM)
            t.append(alias, style=DIM)
        else:
            t.append(f"{_SLOT_UNSET} · ", style=DEEP_DIM)
            t.append_text(self._picker_fallback_desc(level))
        return t

    def _picker_active_desc(self) -> Text:
        t = Text(_ACTIVE_DESC_LEAD, style=DEEP_DIM)
        t.append(self._active_level, style=value_style(self._active_level))
        return t

    def _open_value_picker(self, row: dict) -> None:
        """Список значений строки слота/привязки — ПОД ней (решение 9).

        Слот указывает на алиас реестра, привязка — ещё и на уровень (индирекция
        через слоты). Первая позиция обеих — снятие значения: отдельной клавиши
        снятия нет (Global Constraints), а «не задан»/«активная» — не значения, а
        отсутствие ключа в файле. `on_pick` получает `None` именно за неё —
        сентинел опознаётся строкой с `value is None` и до записи доезжает тем же
        `None`, которым снимает ключ слой записи.

        Уровни и алиасы идут сплошным списком, без черт-разделителей: группы и так
        различимы правой колонкой («→ модель» против «id + адрес») и цветом
        подписи (ревизия владельца — черты внутри дропдауна резали список)."""
        anchor = self._row_widget(self._idx)
        model_w = self._model_width()
        alias_items = [self._picker_alias_item(a, model_w) for a in self._aliases]
        if row["kind"] == "slot":
            level = row["level"]
            rows = [{"value": None, "label": _SLOT_UNSET,
                     "desc": self._picker_fallback_desc(level)},
                    *alias_items]
            self._open_picker(rows, lambda chosen: self._write_binding(
                ("write_slot", level, chosen), self._slot_notice(level, chosen)),
                after=anchor, current=self._slot_value(level))
            return
        consumer = row["consumer"]
        rows = ([{"value": None, "label": _ASSIGN_ACTIVE,
                  "desc": self._picker_active_desc()}]
                + [{"value": lvl, "label": lvl, "desc": self._picker_level_desc(lvl)}
                   for lvl in LEVELS]
                + alias_items)
        self._open_picker(rows, lambda chosen: self._write_binding(
            ("write_assign", consumer, chosen),
            self._assign_notice(consumer, chosen)),
            after=anchor, current=self._assign_value(consumer))

    def _open_picker(self, rows: list, on_pick, *, hint: str | None = None,
                     after=None, current=None) -> None:
        """Строки — словари `{"value", "label", "desc"}`; `value is None` — снятие
        ключа.

        Курсор открывается на ТЕКУЩЕМ значении строки (`current`): выбранное видно
        сразу, Enter без движения — no-op записи того же значения. Ключа в файле
        нет — `current=None` находит первую строку (она и есть «снято»).

        `hint` у пикеров значений НЕ монтируется: каждая позиция объясняет себя
        своей правой колонкой, а общий текст под списком говорил бы только про
        первую строку. Остался он один — вопрос формы «какая модель по умолчанию»,
        там hint и есть сам вопрос."""
        idx = next((i for i, r in enumerate(rows) if r["value"] == current), 0)
        self._picker = {"rows": rows, "idx": idx, "on_pick": on_pick}
        # Строка-якорь на время раскрытия теряет нижний воздух (.set-spaced):
        # список — продолжение строки и обязан стоять к ней вплотную.
        if after is not None:
            after.add_class("models-picker-anchor")
        scroll = VerticalScroll(classes="dropdown-scroll onb-dyn models-picker")
        scroll.can_focus = False
        self.mount(scroll, after=after)
        if not scroll.is_attached:
            return
        scroll.mount(*[Static("", classes="onb-option onb-dyn models-picker-row")
                       for _ in rows])
        if hint is not None:
            self.mount(Static(hint, classes="onb-hint onb-dyn models-picker-hint",
                              markup=False), after=scroll)
        self._refresh_picker_rows()

    def _refresh_picker_rows(self) -> None:
        if self._picker is None:
            return
        rows = self._picker["rows"]
        widgets = list(self.query(".models-picker-row"))
        label_w = max([len(r["label"]) for r in rows] + [0])
        for i, (widget, row) in enumerate(zip(widgets, rows)):
            selected = i == self._picker["idx"]
            text = Text()
            text.append("▸ " if selected else "  ", style=ACCENT)
            # Снятие — по `value is None`, не по тексту подписи: алиас с именем
            # «не задан» схемой разрешён.
            unset = row["value"] is None
            text.append(row["label"].ljust(label_w),
                        style=DEEP_DIM if unset else value_style(row["label"]))
            desc = row.get("desc")
            if desc is not None and desc.plain:
                text.append("  ")
                text.append_text(desc)
            widget.update(text)
            widget.set_class(selected, "selected")
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
        row = picker["rows"][picker["idx"]]
        picker["on_pick"](row["value"])

    def _close_picker(self) -> None:
        self._picker = None
        for widget in self.query(".models-picker, .models-picker-hint"):
            widget.remove()
        for widget in self.query(".models-picker-anchor"):
            widget.remove_class("models-picker-anchor")

    # --- связь с экраном ------------------------------------------------------
    def _notice(self, text: str, *, ok) -> None:
        if self._on_notice is not None:
            self._on_notice(text, ok=ok)

    def _changed(self) -> None:
        if self._on_change is not None:
            self._on_change()
