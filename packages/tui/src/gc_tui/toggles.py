"""Тогглы командной палитры: витрина и переключение.

Конфиг — единственный источник истины; палитра — витрина. Действующее значение
ключа считает его владелец (tui/session_settings.py) — здесь только читаем, чтобы
витрина и сравнение с умолчанием не разошлись правилами."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from gc_core.config_modules.model_registry import (
    configured_choice,
    effective_choice,
)
from gc_core.security.permission_mode import AVAILABLE_MODES
from gc_core.settings import runtime_of, settings_of
from rich.text import Text

from gc_tui import palette as _pal
from gc_tui.services.mode_pill import MODE_HINTS, mode_color
from gc_tui.session_settings import (
    SESSION_KEYS,
    finish_gate_mode_of,
    text_answer_mode_of,
)

# Константы вместо повторяющихся литералов (Sonar S1192).
_TUI_FINISH_GATE_MODE = "tui.finish_gate_mode"
_TUI_TEXT_ANSWER_MODE = "tui.text_answer_mode"


def tui_agent_settings(loaded):
    """Настройки агента TUI поверх действующих: промпт-ассеты, адаптивный bounce
    текстового ответа (`tui.text_answer_mode`) и проекция режима /gate.

    Принимает `LoadedSettings` и отдаёт новый. Проекция /gate — только здесь:
    CLI/server/swarm её не делают, у них глобальный
    `agent.strict_turn_finish_validation` как был и `finish_gate_smart` нет вовсе.
    strict остаётся булевым (mode != "off" → судья потенциально включён), smart —
    отдельным рантайм-сигналом (спека §3): guard читает `finish_gate_smart` +
    `State.turn_tool_calls`."""
    from gc_tui.services.prompt_assets import tui_special_instructions_path

    instructions = tui_special_instructions_path()
    mode = finish_gate_mode_of(loaded.settings)
    return loaded.with_overrides(
        {
            "paths.chat_completions_special_instructions_path": instructions,
            "agent.strict_turn_finish_validation": mode != "off",
        },
        layer="runtime",
    ).with_runtime(
        text_answer_mode=text_answer_mode_of(loaded.settings),
        finish_gate_smart=mode == "smart",
    )


# --- /rigor: пресет как витрина над двумя ключами (спека §2, §6) --------------
# Пресет НЕ хранится (производная). Пара — те же два существующих ключа сессии/
# глобала; SESSION_KEYS их не расширяет — /rigor лишь ставит и резолвит.
RIGOR_KEYS = (_TUI_FINISH_GATE_MODE, _TUI_TEXT_ANSWER_MODE)
PRESET_CUSTOM = "custom"

PRESETS: Dict[str, Dict[str, str]] = {
    "low": {_TUI_FINISH_GATE_MODE: "off", _TUI_TEXT_ANSWER_MODE: "allow"},
    "medium": {_TUI_FINISH_GATE_MODE: "smart", _TUI_TEXT_ANSWER_MODE: "judge"},
    "high": {_TUI_FINISH_GATE_MODE: "on", _TUI_TEXT_ANSWER_MODE: "deny"},
}


def resolve_preset(values: Dict[str, str]) -> str:
    """Имя пресета, чья пара совпала с ``values``, иначе ``PRESET_CUSTOM``.
    Пресеты не пересекаются (тест), поэтому совпадение единственно."""
    for name, pair in PRESETS.items():
        if all(values.get(k) == v for k, v in pair.items()):
            return name
    return PRESET_CUSTOM


def resolve_rigor_state(settings: Any) -> str:
    """Резолв /rigor от ЭФФЕКТИВНЫХ значений пары в настройках (то же правило
    коллизии, что у витрины — через SESSION_KEYS[k].effective_value).
    state-callable /rigor; `settings` — всё, что принимает `settings_of`."""
    values = {k: SESSION_KEYS[k].effective_value(settings) for k in RIGOR_KEYS}
    return resolve_preset(values)


@dataclass(frozen=True)
class ToggleValue:
    """Одно значение тоггла: каноническое имя + подсказка (§5 спеки) + цвет ползунка
    (§3 спеки; токен-константа tui/palette.py). У ручки-шкалы цвета идут от
    слабого к сильному: INK, ACCENT, RUN_COLOR (дизайн-код, «Шкала значений»)."""

    value: str
    hint: str
    color: str


@dataclass(frozen=True)
class ToggleSpec:
    """Спецификация тоггла палитры. `label`/`summary` — витрина (описание в палитре
    `f"{label}: {summary}"`); `dotted` — куда пишет single-key тоггл (пусто у
    preset-тогглов); `state` — читатель эффективного значения; `palette_default` —
    показывать ли в пустом фильтре палитры (спека §4.1: /gate,/answers — False);
    `preset_keys` — непусто ⇔ тоггл-пресет над этими ключами (значение = имя пресета
    от резолвера, применение раскладывается в пару; спека §6).

    Профили v2 (спека §12.1, group «чем работаем» — `/profile`):
    `group` — семья тоггла для потребителей, которым нужна ТОЛЬКО «rigor»-семья
    (Settings «Режимы работы» — секция про самопроверку/формат ответа, не про
    модели; см. `group_toggle_names`); дефолт `"rigor"` — существующие
    gate/answers/rigor ничего не меняют, только новые `models`-тогглы указывают
    свой. `show_track` — False у trackless-тоггла (`/profile`: произвольное
    число профилей, шкала не имеет смысла — сразу значение-имя, спека §12.1).
    `values_provider(app, settings) -> значения` — состав для переданных настроек
    (база окна для Settings или действующие для палитры) вместо
    статического `values` (профили из файлов, а не фиксированный список);
    `hint_provider(app, value) -> текст` — динамический hint вместо статического
    `ToggleValue.hint` (уровни модели для `model`; состав профиля для
    `/profile`) — оба переносятся с прототипа `feat/model-profiles`
    (адаптация под flat-группы ревизий 5-7: без `group`-based разделителей —
    граница «models»→«rigor» уже покрыта существующим правилом «черта перед
    preset-родителем, который не первый», т.к. `/rigor` сам такой родитель).
    `session_switch_profile` — True у `/profile`: применение идёт через
    `app.switch_session_profile` (пересборка + notice + предохранитель,
    спека §9.1), не generic `apply_session_override`."""

    values: Tuple[ToggleValue, ...]
    # Читатель действующего значения: настройки окна (`LoadedSettings`).
    state: Callable[[Any], str]
    label: str
    summary: str
    dotted: str = ""
    palette_default: bool = True
    preset_keys: Tuple[str, ...] = ()
    group: str = "rigor"
    # Подпись строки в Настройках, когда она обязана отличаться от `label`.
    # Палитра и Настройки правят РАЗНОЕ: `/model` крутит сессию, секция пишет в
    # файл — уточнение «по умолчанию» верно только во втором и соврало бы в первой.
    settings_label: str = ""
    show_track: bool = True
    # Trackless: чем показать пустое значение (у /profile «без профиля» —
    # осмысленное состояние, не «не задано», ревизия 2026-07-28).
    empty_value_text: str = "—"
    values_provider: Optional[
        Callable[[Any, Any], Tuple[ToggleValue, ...]]
    ] = None
    hint_provider: Optional[Callable[[Any, str], str]] = None
    # Значение выбирается списком, а не шагом ←/→ (`model`: строка
    # «Режимов работы» открывает пикер моделей).
    opens_picker: bool = False
    # Значение строки в Настройках, когда оно не имя позиции трека:
    # `(app, settings) -> Text` (`model`: знак модели).
    value_text: Optional[Callable[[Any, Any], Text]] = None
    # То же значение словами для серой сводки превью меню, где цветной знак
    # не читается: `(app, settings) -> str` (`model`: имя без знака).
    value_words: Optional[Callable[[Any, Any], str]] = None
    session_switch_profile: bool = False
    # Значение, действующее в сессии, когда оно живёт не в переопределениях
    # конфига (режим разрешений: Shift+Tab, флаг запуска): `(app) -> значение`.
    # Без него действующее значение читается по настройкам окна.
    session_state: Optional[Callable[[Any], str]] = None

    @property
    def names(self) -> Tuple[str, ...]:
        return tuple(v.value for v in self.values)


# --- Роли уровней: подписи слотов в Настройках ------------------------------
LEVEL_ROLE_TEXT: Dict[str, str] = {
    "light": "быстрая и дешёвая: простые задачи",
    "default": "основная рабочая: повседневные задачи",
    "max": "самая сильная: сложные задачи",
}


def auto_unavailable_reason_cfg(cfg: Any) -> Optional[str]:
    """Причина недоступности `auto` для данного конфига или None (спека
    автороутинга §3): единственная точка чтения решения ядра
    (`auto_routing.levels.auto_unavailable_reason`) в TUI — состав значений,
    витрина state, хинт тоггла и другие витрины TUI за пределами этого модуля
    (`app.py::display_router`, `app.py::auto_mode_active`,
    `screens/config.py::_effective_model_name`) спрашивают отсюда или через
    `auto_available_cfg`, условие не дублируют.
    `force_legacy` — тем же детектом, что и стартовый нотис."""
    from gc_core.auto_routing.levels import auto_unavailable_reason
    from gc_core.config import cli_forces_legacy_model

    return auto_unavailable_reason(
        cfg, force_legacy=cli_forces_legacy_model(cfg)
    )


def auto_available_cfg(cfg: Any) -> bool:
    """Доступен ли выбор `auto` для данного конфига."""
    return auto_unavailable_reason_cfg(cfg) is None


def shown_choice(settings: Any) -> str:
    """Выбор модели для витрин (палитра, пикер, `/status`, Настройки):
    `auto` — только пока автовыбор доступен (иначе работает `default`);
    алиас вне реестра — `default`. `settings` — всё, что принимает
    `settings_of`."""
    cfg = settings_of(settings)
    choice = effective_choice(configured_choice(cfg), cfg)
    if choice == "auto" and not auto_available_cfg(settings):
        return "default"
    return choice


def _model_hint(app: Any, value: str) -> str:
    from gc_tui.services.model_picker import model_hint

    return model_hint(app, value)


def _model_value(app: Any, settings: Any) -> Text:
    from gc_tui.services.model_picker import model_value

    return model_value(app, settings)


def _model_words(app: Any, settings: Any) -> str:
    from gc_tui.services.model_picker import model_name

    return model_name(app, settings)


# --- /profile: trackless-строка, циклический выбор файла-профиля -----------
_PROFILE_EMPTY_HINT = "профили не настроены · создать: ~/.gc/profiles/<имя>.yml · пример — в /settings → Профили"
_LEVEL_ORDER = {"light": 0, "default": 1, "max": 2}


def _profile_roots(app: Any) -> list:
    import os
    from pathlib import Path

    from gc_core.config import resolve_config_path
    from gc_core.config_modules.profiles import profile_roots

    config_path = getattr(app, "_config_path", None)
    base_path = Path(resolve_config_path(config_path))
    return profile_roots(Path(os.getcwd()), base_path)


def _discovered_profiles(app: Any) -> Dict[str, Any]:
    from gc_core.config_modules.profiles import discover_profiles

    return discover_profiles(_profile_roots(app))


def profile_values(
    app: Any,
    settings: Any = None,  # noqa: ARG001  # сигнатура values_provider(app, settings)
) -> Tuple[ToggleValue, ...]:
    """Алфавитный объединённый список профилей (спека §3.1/§12.1): тёзка — одна
    позиция (`discover_profiles` уже разрешает коллизию в пользу проектного).
    Цвет/hint не несут смысла здесь — trackless, `hint_provider` динамический."""
    return tuple(
        ToggleValue(name, "", _pal.INK)
        for name in sorted(_discovered_profiles(app))
    )


def _model_names(models: Any) -> Dict[str, str]:
    """Алиас → провайдерский id модели: записи реестра настроек (модели) либо
    сырого документа файла профиля (словари)."""
    names: Dict[str, str] = {}
    for alias, entry in (models or {}).items():
        model = (
            entry.get("model")
            if isinstance(entry, dict)
            else getattr(entry, "model", None)
        )
        names[alias] = str(model or "")
    return names


def _settings_slots(settings: Any) -> Dict[str, Any]:
    """Слоты уровней из настроек — словарём, как в документе профиля."""
    return settings_of(settings).slots.model_dump()


def _profile_slots_summary(
    slots: Dict[str, Any], models: Dict[str, str]
) -> str:
    """«light m1 · default m2»: уровни со слотом, чей алиас есть в реестре
    (`models` — алиас → id модели, см. `_model_names`)."""
    pairs = []
    for level, alias in (slots or {}).items():
        if alias not in models:
            continue
        pairs.append((level, f"{level} {models[alias] or alias}"))
    pairs.sort(key=lambda pair: _LEVEL_ORDER.get(pair[0], 99))
    return " · ".join(text for _, text in pairs)


def _profile_mcp_rebuild_note(app: Any, value: str) -> str:
    if _discovered_profiles(app).get(value) is None:
        return ""
    # Читатель ОДИН на оба хинта (`_profile_file_doc`): вторая копия чтения жила
    # рядом и разошлась с первой — она не знала про чужую кодировку и роняла
    # палитру трейсбеком на одном наведении курсора, пока первую уже чинили.
    # Битый или нечитаемый файл (человек правит руками) даёт пустой документ:
    # предупреждение о MCP просто не покажется.
    raw = _profile_file_doc(app, value)
    # Во ВХОДНОМ документе секция называется `mcp: servers:` — литеральный
    # верхнеуровневый `mcp-servers:` схема профиля отвергает (docs/configuration.md).
    mcp = raw.get("mcp") if isinstance(raw, dict) else None
    servers = mcp.get("servers") if isinstance(mcp, dict) else None
    if not servers:
        return ""
    return f"переключение пересоберёт MCP: {len(servers)} серверов"


def profile_value_hint(app: Any, value: str) -> str:
    """Динамический hint `/profile` (спека §12.1): состав слотов + источник
    (проектный/глобальный) +, если целевой профиль переопределяет `mcp.servers`,
    предупреждение цены переключения. Пустое состояние — текст спеки дословно."""
    if not value:
        # Критерий пустого состояния — СУЩЕСТВОВАНИЕ профилей, не «не выбран»
        # (ревизия 2026-07-28: хинт «не настроены» при живом smoke.yml — баг смока).
        names = sorted(_discovered_profiles(app))
        if not names:
            return _PROFILE_EMPTY_HINT
        return f"не выбран · доступно: {', '.join(names)} · Enter — открыть список"
    settings = _effective_settings(app)
    ref = _discovered_profiles(app).get(value)
    parts = []
    summary = _profile_slots_summary(
        _settings_slots(settings), _model_names(settings_of(settings).models)
    )
    if summary:
        parts.append(summary)
    if ref is not None:
        parts.append(
            "проектный" if ref.root_kind == "project" else "глобальный"
        )
    note = _profile_mcp_rebuild_note(app, value)
    if note:
        parts.append(note)
    parts.append("Enter — открыть список")
    return " · ".join(parts)


PROFILE_NONE_VALUE = "без профиля"


def profile_name(app: Any) -> str:
    """Действующий профиль окна: имя или «без профиля» — значение палитры
    и стартовая строка пикера `/profile`."""
    return toggle_state(app, "profiles") or PROFILE_NONE_VALUE


def profile_value(app: Any) -> Text:
    """Значение `/profile` в правой колонке палитры."""
    return Text(profile_name(app))


_PROFILE_NONE_HINT = (
    "настройки берутся только из config.yml — файл профиля не подмешивается"
)
# Отметка действующего профиля — одна на пикер `/profile` и секцию «Профили»
# /settings (`_profile_row_text`/`_none_row_text`): зелёное слово (OK)
# колонкой сразу за именем (решение смока 2026-10-25).
PROFILE_ACTIVE_STATUS = "активен"
# Тот же вердикт, что у гейта выбора (`TuiApp.probe_profile_assembly`), но ДО
# нажатия: отказ после Enter — половина дела, человек уже успел выбрать
# (решение владельца по живому смоку).
_PROFILE_BROKEN_STATUS = "✗ не применится"


def _profile_assembly_error(app: Any, name: str) -> str:
    """Причина, по которой конфиг с профилем `name` не собирается («» — собирается).

    Судья ОДИН на витрину и на гейт выбора: вторая редакция правила «годен ли
    профиль» разошлась бы с первой, и пикер звал бы Enter туда, где Enter
    отказывает. Метод достаётся ДО try: его отсутствие — поломка сборки, а не
    вердикт о профиле, и молча красить им все строки нельзя."""
    probe = app.probe_profile_assembly
    try:
        probe(name)
    except Exception as exc:
        # Любое исключение = не соберётся. Сужать до ValueError нечем: причина
        # приходит из всей цепочки загрузки конфига, и незнакомый тип означал бы
        # краш палитры на одном лишь открытии списка.
        return str(exc)
    return ""


def _profile_verdict_key(app: Any, name: str) -> Optional[tuple]:
    """Снимок мтаймов файлов сборки профиля `name` (`config.profile_mtime_set`,
    голый `stat()`, без парсинга) — ключ кэша `_cached_assembly_error`. Не
    удалось выяснить состав (`OSError`) — `None`: кэш такой пробы не хранит,
    решение то же, что у `_config_stack_texts`/`_models_verdict`
    (`settings.py`) для вердикта секции «Модели»."""
    from gc_core.config import profile_mtime_set

    try:
        mtimes = profile_mtime_set(getattr(app, "_config_path", None), name)
    except OSError:
        return None
    return tuple(sorted(mtimes.items()))


def _cached_assembly_error(app: Any, name: str) -> str:
    """`_profile_assembly_error(app, name)`, посчитанная не чаще, чем меняется
    хоть один файл сборки этого профиля — общий кэш для витрины списка
    (`profile_picker_options`, один проход на открытие) И хинта подвала
    (`profile_picker_hint`): какая из них первой спросила про имя, та и платит
    единственную полную сборку (`probe_profile_assembly` → `load_settings`,
    ~6 мс), вторая берёт готовый ответ. Без этого хинт пересобирал конфиг
    заново на каждый highlight."""
    key = _profile_verdict_key(app, name)
    cache: Dict[str, tuple] = (
        getattr(app, "_profile_assembly_cache", None) or {}
    )
    cached = cache.get(name)
    if key is not None and cached is not None and cached[0] == key:
        return cached[1]
    reason = _profile_assembly_error(app, name)
    if key is not None:
        cache[name] = (key, reason)
        app._profile_assembly_cache = cache
    return reason


def profile_picker_options(app: Any) -> Tuple[Tuple[str, str, str], ...]:
    """Опции пикера `/profile` (спека §12.1, ревизия 2026-07-28; статус-колонка —
    задача 10, решение смока 2026-08-10): первым — «— без профиля» (сессионное
    снятие, §9.1), затем профили по алфавиту объединённого списка. 3-tuple
    (value, описание, статус): описание — источник + «(как в конфиге)» у
    профиля конфиг-указателя; статус — «активен» ОТДЕЛЬНЫМ элементом (не
    частью описания) у ровно одной строки — той, что действует сейчас
    (`ArgPicker` ставит его колонкой за именем, см. `arg_picker.py`).

    Второй статус — «✗ не применится» у профиля, с которым конфиг не собирается
    (тот же судья, что у гейта выбора). Действующий профиль его не получает,
    даже если его файл испортили под ногами: он УЖЕ применён и работает, а
    «не применится» звало бы чинить выбор, который никуда не переключается —
    статус описывает текущее состояние, и оно приоритетнее. Два статуса в одной
    колонке не показываем: колонка одна, и вторая метка вытеснила бы первую.

    Проба стоит ~6 мс на профиль — но не на КАДР: `_cached_assembly_error`
    делит её с хинтом подвала (`profile_picker_hint`), и за одно открытие
    списка профиль пробуется не больше раза, кто бы из двух его первым ни
    спросил. Сам этот провайдер зовётся один раз на открытие (`main.py`,
    ветка `if self._arg_picker is None`) — стрелки и фильтр идут через
    `set_filter` по готовым опциям; хинт, в отличие от них, спрашивается на
    каждый highlight — без общего кэша с этим провайдером он звал бы пробу
    заново на каждом."""
    refs = _discovered_profiles(app)
    effective = runtime_of(_effective_settings(app)).active_profile or ""
    base_pointer = runtime_of(_global_settings(app)).active_profile or ""
    none_status = PROFILE_ACTIVE_STATUS if not effective else ""
    options = [(PROFILE_NONE_VALUE, "работать чистым конфигом", none_status)]
    for name in sorted(refs):
        ref = refs[name]
        desc = "проектный" if ref.root_kind == "project" else "глобальный"
        if name == base_pointer:
            desc += " (как в конфиге)"
        if name == effective:
            status = PROFILE_ACTIVE_STATUS  # действующий уже собрался — пробы не надо
        else:
            status = (
                _PROFILE_BROKEN_STATUS
                if _cached_assembly_error(app, name)
                else ""
            )
        options.append((name, desc, status))
    return tuple(options)


def _profile_file_doc(app: Any, name: str) -> Dict[str, Any]:
    """Сырой документ файла профиля (для хинтов ЦЕЛЕВОГО, ещё не применённого
    профиля — эффективный конфиг тут не годится: выбор применяется только по
    Enter). Битый файл → {} (хинт без состава, не краш)."""
    ref = _discovered_profiles(app).get(name)
    if ref is None:
        return {}
    import yaml
    from gc_core.utils.user_text import read_user_text_or_none

    # Нечитаемый файл роняет палитру на одном наведении курсора (живой смок):
    # хинт строится на подсветке пункта, до всякого выбора.
    try:
        raw = yaml.safe_load(read_user_text_or_none(ref.path) or "") or {}
    except yaml.YAMLError:
        return {}
    return raw if isinstance(raw, dict) else {}


def profile_picker_hint(app: Any, value: Any) -> str:
    """Хинт подвала пикера для подсвеченного пункта (спека §12.1): состав слотов
    ЦЕЛЕВОГО профиля из его файла + источник + MCP-предупреждение; для пункта
    снятия — честное пояснение."""
    if value in (None, "", PROFILE_NONE_VALUE):
        return _PROFILE_NONE_HINT
    name = str(value)
    ref = _discovered_profiles(app).get(name)
    doc = _profile_file_doc(app, name)
    # Модели для подписи слота: из файла профиля, с фоллбэком на эффективный
    # реестр (профиль может ссылаться на алиасы нижних слоёв).
    effective_models = _model_names(
        settings_of(_effective_settings(app)).models
    )
    doc_models = doc.get("models")
    merged_models = {
        **effective_models,
        **_model_names(doc_models if isinstance(doc_models, dict) else {}),
    }
    parts = []
    # Статус строки говорит ЧТО («✗ не применится»), подсказка — ПОЧЕМУ. Причина
    # идёт ПЕРВОЙ и целиком: текст ядра самодостаточен («<путь>: …»), а рядом с
    # палитрой путь профиля не печатает никто — без него нечего чинить.
    reason = _cached_assembly_error(app, name)
    if reason:
        parts.append(reason)
    doc_slots = doc.get("slots")
    summary = _profile_slots_summary(
        doc_slots if isinstance(doc_slots, dict) else {}, merged_models
    )
    if summary:
        parts.append(summary)
    if ref is not None:
        parts.append(
            "проектный" if ref.root_kind == "project" else "глобальный"
        )
    note = _profile_mcp_rebuild_note(app, name)
    if note:
        parts.append(note)
    return " · ".join(parts) if parts else "—"


def group_toggle_names(group: str) -> list:
    """Имена тогглов данной группы, в порядке реестра `TOGGLES`."""
    return [n for n, s in TOGGLES.items() if s.group == group]


# Состав секции «Режимы работы» — упорядоченный список ГРУПП, а не одна группа.
# Порядок блоков сверху вниз — решение владельца: «чем работаем» (модель по
# умолчанию), «что агенту можно» (режим разрешений по умолчанию), «как строго
# закрывать ход» (строгость, самопроверка, формат ответа).
MODES_SECTION_GROUPS: Tuple[str, ...] = ("model", "permissions", "rigor")


def modes_section_names() -> list:
    """Имена строк секции «Режимы работы» в порядке показа.

    Секция НЕ равна одной группе: `model` правит ключ `model:` конфига и
    стоит здесь же, отдельным блоком под чертой (к профилям он отношения не
    имеет — там правился чужой ключ). Знание о композиции секции живёт в одном
    месте: и рендер, и навигация, и запись, и превью меню спрашивают отсюда."""
    return [
        n for group in MODES_SECTION_GROUPS for n in group_toggle_names(group)
    ]


def _toggle_values_for(
    app: Any, name: str, settings: Any
) -> Tuple[ToggleValue, ...]:
    spec = TOGGLES[name]
    if spec.values_provider is not None:
        return spec.values_provider(app, settings)
    return spec.values


def effective_toggle_values(app: Any, name: str) -> Tuple[ToggleValue, ...]:
    """Состав значений тоггла по действующим настройкам окна (база + сессия) —
    палитра и всё, что показывает текущий контекст."""
    return _toggle_values_for(app, name, _effective_settings(app))


def effective_toggle_names(app: Any, name: str) -> Tuple[str, ...]:
    return tuple(v.value for v in effective_toggle_values(app, name))


def base_toggle_values(app: Any, name: str) -> Tuple[ToggleValue, ...]:
    """Состав значений для секции Settings — по глобальной базе без сессии, тот же
    источник, что `toggle_state_base`: значение и его доступность из одного места."""
    return _toggle_values_for(app, name, _global_settings(app))


def base_toggle_names(app: Any, name: str) -> Tuple[str, ...]:
    return tuple(v.value for v in base_toggle_values(app, name))


TOGGLES: Dict[str, ToggleSpec] = {
    "model": ToggleSpec(
        dotted="model",
        values=(),
        state=shown_choice,
        label="модель",
        summary="auto или конкретная из реестра",
        group="model",
        settings_label="модель по умолчанию",
        show_track=False,
        hint_provider=_model_hint,
        opens_picker=True,
        value_text=_model_value,
        value_words=_model_words,
        palette_default=False,
    ),
    "profiles": ToggleSpec(
        dotted="profile",
        values=(),
        values_provider=profile_values,
        state=lambda settings: str(runtime_of(settings).active_profile or ""),
        label="профили",
        summary="переключаемые наборы настроек",
        group="models",
        show_track=False,
        empty_value_text=PROFILE_NONE_VALUE,
        hint_provider=profile_value_hint,
        session_switch_profile=True,
        palette_default=False,
    ),
    # Только Настройки: команды палитры нет — режим ТЕКУЩЕЙ сессии переключает
    # Shift+Tab. Строка правит конфиг; сессия без своего выбора следует ему
    # сразу (правило «выбор сессии → конфиг», `mode_with_source`).
    "permissions": ToggleSpec(
        dotted="security.permissions.default_mode",
        values=tuple(
            ToggleValue(
                mode.value,
                f"{MODE_HINTS[mode.value]} · в сессии — Shift+Tab",
                mode_color(mode.value),
            )
            for mode in AVAILABLE_MODES
        ),
        state=lambda settings: str(
            settings_of(settings).security.permissions.default_mode
        ),
        label="режим разрешений",
        summary="что агенту можно без вопроса",
        group="permissions",
        settings_label="режим разрешений по умолчанию",
        palette_default=False,
        session_state=lambda app: app.effective_permission_mode(),
    ),
    "rigor": ToggleSpec(
        values=(
            ToggleValue(
                "low",
                "без самопроверки, ответ можно дать текстом · быстро и дёшево: простые задачи и вопросы",
                _pal.INK,
            ),
            ToggleValue(
                "medium",
                "самопроверка после работы с инструментами, текст — по решению самопроверки · универсальный режим",
                _pal.ACCENT,
            ),
            ToggleValue(
                "high",
                "самопроверка всегда, завершать только вызовом инструмента · сложные задачи, где важна полнота",
                _pal.RUN_COLOR,
            ),
        ),
        state=resolve_rigor_state,
        label="строгость завершения хода",
        summary="самопроверка и формат ответа",
        preset_keys=RIGOR_KEYS,
        palette_default=True,
    ),
    "gate": ToggleSpec(
        dotted=_TUI_FINISH_GATE_MODE,
        values=(
            ToggleValue(
                "off",
                "самопроверка выключена · быстрее: для простых задач и экономии токенов",
                _pal.INK,
            ),
            ToggleValue(
                "smart",
                "самопроверка только если агент работал инструментами · разговор без действий не проверяется",
                _pal.ACCENT,
            ),
            ToggleValue(
                "on",
                "самопроверка не даст закончить, пока запрос не закрыт · для сложных задач, где важна полнота",
                _pal.RUN_COLOR,
            ),
        ),
        state=finish_gate_mode_of,
        label="самопроверка выполнения задачи",
        summary="закрыт ли запрос",
        palette_default=False,
    ),
    "answers": ToggleSpec(
        dotted=_TUI_TEXT_ANSWER_MODE,
        values=(
            ToggleValue(
                "allow",
                "текстовый ответ всегда принимается · для чата и быстрых ответов",
                _pal.INK,
            ),
            ToggleValue(
                "judge",
                "текстовый ответ принимается, если проходит самопроверку · универсальный режим",
                _pal.ACCENT,
            ),
            ToggleValue(
                "deny",
                "текст не завершает ход: агент обязан закончить вызовом инструмента · для строгой автономии",
                _pal.RUN_COLOR,
            ),
        ),
        state=text_answer_mode_of,
        label="формат ответа",
        summary="допустим ли текст как финал хода",
        palette_default=False,
    ),
}


_HINT_KEYS_TOGGLE = "←/→ — изменить · Enter — готово"
_HINT_KEYS_TOGGLE_MEDIUM = "←/→ — изменить"
_HINT_KEYS_TOGGLE_SHORT = "←/→"


def _fit_hint(value: str, hint: str, keys: str, width: int) -> str:
    """Приоритетное усечение hint-строки тоггла (канон §6.1 спеки уровней
    настроек 2026-07-21, 4 ступени): 1) хвост подсказки после последнего
    ' · ' уходит первым; 2) клавиши теряют «Enter — готово» (ЦЕЛЫМ куском,
    не обрывком); 3) клавиши сжимаются до `←/→`; 4) остаток смысла —
    многоточие.
    Механика (клавиши в той или иной форме) видима всегда. Settings
    подсказку не режет: показывает целиком с резервом высоты (дизайн-код
    §9).

    Владелец канонического представления тоггла (рядом с render_track) —
    переиспользуется палитрой (command_palette.py)."""

    def build(h: str, k: str) -> str:
        return f"{value} — {h}  ·  {k}"

    if width <= 0 or len(build(hint, keys)) <= width:
        return build(hint, keys)
    core = hint.rsplit(" · ", 1)[0]  # 1) без «кому подходит»
    if len(build(core, keys)) <= width:
        return build(core, keys)
    if len(build(core, _HINT_KEYS_TOGGLE_MEDIUM)) <= width:  # 2) без области
        return build(core, _HINT_KEYS_TOGGLE_MEDIUM)
    if (
        len(build(core, _HINT_KEYS_TOGGLE_SHORT)) <= width
    ):  # 3) короткие клавиши
        return build(core, _HINT_KEYS_TOGGLE_SHORT)
    fixed = len(build("", _HINT_KEYS_TOGGLE_SHORT))
    room = max(1, width - fixed)
    return build(
        core[: room - 1] + "…", _HINT_KEYS_TOGGLE_SHORT
    )  # 4) многоточие


def render_track(
    spec: ToggleSpec,
    current: str,
    *,
    extra_lit: Any = None,
    values: Optional[Tuple[ToggleValue, ...]] = None,
) -> Text:
    """Канонический трек тоггла (спека §2–§3): ● на позиции current цветом
    значения (bold), пустые позиции — ○ DEEP_DIM. `values` — состав для
    рендера, по умолчанию `spec.values`. `extra_lit` (спека §4.1, /rigor,
    ревизия 4) — отдельный вне-шкальный глиф custom после двойного пробела
    (значение не входит в трек `values`): None — зоны нет; False — зона
    ЗАРЕЗЕРВИРОВАНА, но пуста (без тусклой ○ — custom не выбирается
    стрелкой); True — `●` BRAND (custom применена), база вся тусклая.
    Только трек — колонки/подпись у потребителя."""
    values = values or spec.values
    names = tuple(v.value for v in values)
    idx = names.index(current) if current in names else -1
    t = Text()
    for i, val in enumerate(values):
        if i == idx and extra_lit is not True:
            t.append("●", style=f"bold {val.color}")
        else:
            t.append("○", style=_pal.DEEP_DIM)
        if i < len(values) - 1:
            t.append(" ")
    if extra_lit is not None:
        t.append("  ")  # двойной пробел — отделяет позицию custom
        if extra_lit is True:
            t.append("●", style=f"bold {_pal.BRAND}")
        else:
            t.append(" ")  # резерв: custom пуст, без тусклой ○ (ревизия 4)
    return t


def padded_track(
    spec: ToggleSpec,
    current: str,
    *,
    extra_lit: Any,
    base_w: int,
    extra_w: int,
    values: Optional[Tuple[ToggleValue, ...]] = None,
) -> Text:
    """Трек тоггла, выровненный по ЛЕВОМУ краю общего поля (ревизия 5, спека §6:
    системный контракт, владелец здесь — переиспользуют палитра и Settings).
    `render_track` остаётся ЕДИНСТВЕННЫМ рендером — эта функция только паддит
    его результат пробелами СПРАВА до общей ширины поля `base_w + extra_w`.
    Резерв custom стоит вплотную к СВОЕЙ базе, а не в колонке после самой
    длинной базы секции. `values` — состав для `render_track`."""
    values = values or spec.values
    full = render_track(spec, current, extra_lit=extra_lit, values=values)
    t = Text()
    t.append_text(full)
    t.append(" " * (base_w + extra_w - len(full.plain)))
    return t


def toggle_state(app: Any, name: str) -> str:
    """Текущее эффективное значение тоггла `name` (`gate` — через правило
    коллизии, `answers` — сырое значение). Источник — действующие настройки
    (база + сессионные переопределения), не база окна; у тоггла с
    `session_state` — оно (выбор сессии живёт вне конфига)."""
    spec = TOGGLES[name]
    if spec.session_state is not None:
        return spec.session_state(app)
    return spec.state(_effective_settings(app))


def toggle_state_base(app: Any, name: str) -> str:
    """Глобальное (файловое) значение тоггла `name`: из базы окна БЕЗ
    сессионных переопределений — то, что правит секция Settings (в отличие от
    `toggle_state`, отдающего эффективное = база + сессия). Читается тем же
    правилом коллизии (`TOGGLES[name].state`), что и эффективное, — иначе
    строка секции и оверрайд-пометка разошлись бы правилами."""
    return TOGGLES[name].state(_global_settings(app))


def owning_preset_name(toggle_name: str) -> Optional[str]:
    """Имя preset-тоггла, чьим ребёнком является `toggle_name` (его `dotted` ∈
    `preset_keys` пресета), иначе None. Data-родство ребёнок→пресет для
    surface'ов, которым нужна связь БЕЗ цветовой группировки палитры — напр.
    `/help`: текстовый хвост «настройка /<preset>». Generic по данным TOGGLES."""
    spec = TOGGLES.get(toggle_name)
    if spec is None or not spec.dotted:
        return None
    for pname, pspec in TOGGLES.items():
        if pspec.preset_keys and spec.dotted in pspec.preset_keys:
            return pname
    return None


def cycle_in(names: list[str], current: str, direction: int) -> str:
    """Следующее значение по кругу (±1) в произвольном списке `names`, начиная
    от `current`. Единственная точка индексной формулы тоггл-цикла — источник
    списка задаёт вызывающий (`effective_toggle_names` у `next_toggle_value` и
    у `_modes_apply` в settings.py)."""
    idx = names.index(current) if current in names else 0
    return names[(idx + direction) % len(names)]


def apply_toggle(app: Any, name: str, value: str) -> str:
    """Применить `value` тоггла `name` к ТЕКУЩЕЙ СЕССИИ (в config.yml НЕ пишет).

    preset-тоггл (/rigor): значение-имя пресета раскладывается в пару
    (apply_session_overrides). `custom` НЕ применяемое значение (Г-А) — она не
    позиция трека (ревизия 4, custom только отображается), `next_toggle_value`
    её никогда не вернёт; вызов с ней напрямую — ValueError, ничего в сессию
    не пишется. single-key — прежняя запись одного ключа (apply_session_override).
    Откатывается целиком при OSError; вызывающий показывает «✗ не сохранено».

    `/profile` (`session_switch_profile`, спека §9.1) — применяет через
    `app.switch_session_profile` (пересборка + notice + сброс предохранителя —
    отдельная семантика, не generic single-key путь)."""
    spec = TOGGLES[name]
    if spec.preset_keys:
        if value not in PRESETS:
            raise ValueError(
                f"invalid preset value {value!r} for toggle {name!r}, expected one of {tuple(PRESETS)}"
            )
        app.apply_session_overrides(PRESETS[value])
        app.invalidate_runner()
        return toggle_state(app, name)
    names = effective_toggle_names(app, name)
    if value not in names:
        raise ValueError(
            f"invalid value {value!r} for toggle {name!r}, expected one of {names}"
        )
    if spec.session_switch_profile:
        app.switch_session_profile(value)
        return toggle_state(app, name)
    app.apply_session_override(spec.dotted, value)
    app.invalidate_runner()
    return toggle_state(app, name)


def _global_settings(app: Any) -> Any:
    """База окна (`LoadedSettings`) — то, что правит секция Settings."""
    return app.base_settings()


def _effective_settings(app: Any) -> Any:
    """Действующие настройки окна (`LoadedSettings`): база + сессия."""
    return app.effective_settings()


def next_toggle_value(app: Any, name: str, direction: int) -> str:
    """Generic следующий шаг тоггла по кругу — та же формула, что у
    `cycle_in`, но отправная точка читается через эффективное состояние
    (`toggle_state(app, name)`), а не передаётся аргументом: preset-тогглу
    нужен резолв пары через конфиг приложения, single-key — просто текущее
    значение. Custom НЕ позиция трека (ревизия 4) — эффективное значение вне
    `spec.names` (отображаемый custom) трактуется как idx 0, шаг с него по
    кругу уводит на пресет; custom этой функцией никогда не возвращается.

    `/profile` (спека §12.1) — набор значений динамический
    (`effective_toggle_names`, а не статический `TOGGLES[name].names`); пустой
    набор (профили не настроены) — цикл no-op, возвращает текущее значение
    (пустую строку)."""
    names = effective_toggle_names(app, name)
    current = toggle_state(app, name)
    if not names:
        return current
    return cycle_in(names, current, direction)


def preset_track_extra(app: Any, name: str) -> Any:
    """Состояние 4-й позиции трека /rigor (ревизия 3, спека §2, §4.1): для
    preset-тоггла ВСЕГДА не-None (позиция custom видна всегда) — `True` ⇔
    эффективная пара нестандартна (custom применена), иначе `False`. Глобал
    больше не участвует (был отдельный «постоянный» случай — упразднён).
    single-key → `None` (позиции нет вовсе)."""
    spec = TOGGLES[name]
    if not spec.preset_keys:
        return None
    return spec.state(_effective_settings(app)) == PRESET_CUSTOM


def preset_custom_hint(
    app: Any, name: str, *, scope: str = "effective"
) -> str:
    """Динамический custom-хинт, два состояния (ревизия 4, спека §5, тексты
    ДОСЛОВНО) — зовётся, только когда custom реально ОТОБРАЖЁН (эффективная
    пара нестандартна; вызывающий `_toggle_hint` гарантирует это условием
    `current not in spec.names`), различаются источником нестандартности:
    1) хотя бы один ключ пары в сессионных оверрайдах — «своя пара этой
    сессии: …»; 2) оверрайдов пары нет (нестандартен сам глобал) — «ваш
    дефолт: …».

    `scope` (ревьюный follow-up V1, ревизия 4 §4.2): "effective" (палитра,
    дефолт) — значения и рамка из эффективного конфига (база + сессия), как
    описано выше; "global" (Settings) — строка секции правит ГЛОБАЛ, поэтому
    значения и рамка ВСЕГДА от базового конфига, независимо от сессионных
    оверрайдов пары (те уже показаны отдельным хвостом «в текущей сессии»)."""
    spec = TOGGLES[name]
    settings = (
        _global_settings(app)
        if scope == "global"
        else _effective_settings(app)
    )
    x = SESSION_KEYS[spec.preset_keys[0]].effective_value(settings)
    y = SESSION_KEYS[spec.preset_keys[1]].effective_value(settings)
    if scope != "global":
        overrides = getattr(app, "_session_overrides", None) or {}
        if any(k in overrides for k in spec.preset_keys):
            return (
                f"своя пара этой сессии: самопроверка {x}, формат {y} · "
                "шаг на пресет заменит пару · закрепить — в /settings"
            )
    return f"ваш дефолт: самопроверка {x}, формат {y} · менять в /settings"


def dynamic_toggle_hint(app: Any, name: str, value: str) -> str:
    """Инъекция для `CommandPalette(toggle_dynamic_hint=...)` (спека §12.1):
    зовёт `TOGGLES[name].hint_provider(app, value)`. Пустая строка — тоггл без
    `hint_provider` (палитра тогда берёт статический `ToggleValue.hint`)."""
    spec = TOGGLES[name]
    if spec.hint_provider is None:
        return ""
    return spec.hint_provider(app, value)
