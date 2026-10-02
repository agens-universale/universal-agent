"""Тогглы командной палитры: витрина и переключение.

Конфиг — единственный источник истины; палитра — витрина. Действующее значение
ключа считает его владелец (tui/session_settings.py) — здесь только читаем, чтобы
витрина и сравнение с умолчанием не разошлись правилами."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from rich.text import Text

from gc_tui import palette as _pal
from gc_tui.session_settings import (
    SESSION_KEYS,
    effective_finish_gate_mode,
    effective_text_answer_mode,
    finish_gate_mode_of,
    text_answer_mode_of,
)

# Константы вместо повторяющихся литералов (Sonar S1192).
_TUI_FINISH_GATE_MODE = "tui.finish_gate_mode"
_TUI_TEXT_ANSWER_MODE = "tui.text_answer_mode"


def apply_finish_gate_mode(cfg: Dict[str, Any]) -> None:
    """TUI-wiring: спроецировать эффективный режим /gate в runtime-ключи судьи.

    Зовётся ТОЛЬКО из TUI-сборки конфига (tui/app.py::_build_real_executor):
    CLI/server/swarm не зовут — у них глобальный agent.strict_turn_finish_validation
    как был, и ключа finish_gate_smart нет вовсе. strict остаётся булевым
    (mode != "off" → судья потенциально включён), smart — отдельным runtime-ключом
    (спека §3): guard читает finish_gate_smart + State.turn_tool_calls."""
    mode = effective_finish_gate_mode(cfg)
    cfg["strict_turn_finish_validation"] = mode != "off"
    cfg["finish_gate_smart"] = mode == "smart"


def tui_agent_settings(loaded):
    """Настройки агента TUI поверх действующих: промпт-ассеты, адаптивный bounce
    текстового ответа (`tui.text_answer_mode`) и проекция режима /gate.

    Принимает `LoadedSettings` и отдаёт новый; переходно — плоский словарь,
    который правится на месте и возвращается (окна, собранные словарём)."""
    from gc_tui.services.prompt_assets import tui_special_instructions_path

    instructions = tui_special_instructions_path()
    if isinstance(loaded, dict):
        loaded["console_trace"] = False
        loaded["chat_completions_special_instructions_path"] = instructions
        loaded["text_answer_mode"] = loaded.get(
            "tui_text_answer_mode", "judge"
        )
        apply_finish_gate_mode(loaded)
        return loaded
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


def resolve_rigor_state(cfg: Dict[str, Any]) -> str:
    """Резолв /rigor от ЭФФЕКТИВНЫХ значений пары в cfg (то же правило коллизии,
    что у витрины — через SESSION_KEYS[k].effective_value). state-callable /rigor."""
    values = {k: SESSION_KEYS[k].effective_value(cfg) for k in RIGOR_KEYS}
    return resolve_preset(values)


@dataclass(frozen=True)
class ToggleValue:
    """Одно значение тоггла: каноническое имя + подсказка (§5 спеки) + цвет ползунка
    (§3 спеки; токен-константа tui/palette.py)."""

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

    Профили v2 (спека §12.1, group «чем работаем» — `/level`, `/profiles`):
    `group` — семья тоггла для потребителей, которым нужна ТОЛЬКО «rigor»-семья
    (Settings «Режимы работы» — секция про самопроверку/формат ответа, не про
    модели; см. `group_toggle_names`); дефолт `"rigor"` — существующие
    gate/answers/rigor ничего не меняют, только новые `models`-тогглы указывают
    свой. `show_track` — False у trackless-тоггла (`/profiles`: произвольное
    число профилей, шкала не имеет смысла — сразу значение-имя, спека §12.1).
    `values_provider(app) -> значения` — динамический набор значений вместо
    статического `values` (профили из файлов, а не фиксированный список);
    `hint_provider(app, value) -> текст` — динамический hint вместо статического
    `ToggleValue.hint` (модель, резолвнутая Router'ом для `/level`; состав
    профиля для `/profiles`) — оба переносятся с прототипа `feat/model-profiles`
    (адаптация под flat-группы ревизий 5-7: без `group`-based разделителей —
    граница «models»→«rigor» уже покрыта существующим правилом «черта перед
    preset-родителем, который не первый», т.к. `/rigor` сам такой родитель).
    `session_switch_profile` — True у `/profiles`: применение идёт через
    `app.switch_session_profile` (пересборка + notice + предохранитель,
    спека §9.1), не generic `apply_session_override`. `rebuild_on_apply` —
    False у `/level`: единственный тоггл, переключаемый БЕЗ `invalidate_runner`
    (спека §6.4 — уровень фиксируется на ход инжектируемым провайдером, не
    снимком executor)."""

    values: Tuple[ToggleValue, ...]
    state: Callable[[Dict[str, Any]], str]
    label: str
    summary: str
    dotted: str = ""
    palette_default: bool = True
    preset_keys: Tuple[str, ...] = ()
    group: str = "rigor"
    # Подпись строки в Настройках, когда она обязана отличаться от `label`.
    # Палитра и Настройки правят РАЗНОЕ: `/level` крутит сессию, секция пишет в
    # файл — уточнение «по умолчанию» верно только во втором и соврало бы в первой.
    settings_label: str = ""
    show_track: bool = True
    # Trackless: чем показать пустое значение (у /profiles «без профиля» —
    # осмысленное состояние, не «не задано», ревизия 2026-07-28).
    empty_value_text: str = "—"
    values_provider: Optional[Callable[[Any], Tuple[ToggleValue, ...]]] = None
    hint_provider: Optional[Callable[[Any, str], str]] = None
    session_switch_profile: bool = False
    rebuild_on_apply: bool = True

    @property
    def names(self) -> Tuple[str, ...]:
        return tuple(v.value for v in self.values)


# --- /level: трек уровней, хинты из Router (спека §12.1) --------------------
LEVEL_ROLE_TEXT: Dict[str, str] = {
    "light": "быстрая и дешёвая: простые задачи",
    "default": "основная рабочая: повседневные задачи",
    "max": "самая сильная: сложные задачи",
}


def level_value_hint(app: Any, value: str) -> str:
    """Динамический hint позиции `/level` (спека §12.1, копирайт §1): модель,
    резолвнутая Router'ом ДЛЯ ЭТОГО уровня (не обязательно активного —
    `ModelRouter.resolve_level`), + роль уровня. Легаси/легаси-форс — честные
    пометки вместо модели+роли (канон §12.1); фоллбэк уровня — честная стрелка
    «light → default» вместо модели притворного light-слота."""
    from gc_core.model_router import AliasCircuitOpenError

    router = app.display_router()
    try:
        resolved = router.resolve_level(value)
    except AliasCircuitOpenError as exc:
        return (
            f"{exc.alias} недоступна (предохранитель) · переключите /profiles"
        )
    except ValueError:
        return (
            f'уровень "{value}" не резолвится · настройте slots в config.yml'
        )
    if router.force_legacy:
        return f"{resolved.model} · --model: уровни отключены на этот запуск"
    if router.registry_empty():
        return f"{resolved.model} · легаси: уровни появятся с реестром моделей"
    role = LEVEL_ROLE_TEXT.get(value, "")
    if resolved.source.startswith("фоллбэк "):
        chain = resolved.source[len("фоллбэк ") :]
        return f"{resolved.alias} ({chain}) · {role}"
    return f"{resolved.alias} · {role}"


# --- /profiles: trackless-строка, циклический выбор файла-профиля -----------
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


def profile_values(app: Any) -> Tuple[ToggleValue, ...]:
    """Алфавитный объединённый список профилей (спека §3.1/§12.1): тёзка — одна
    позиция (`discover_profiles` уже разрешает коллизию в пользу проектного).
    Цвет/hint не несут смысла здесь — trackless, `hint_provider` динамический."""
    return tuple(
        ToggleValue(name, "", _pal.INK)
        for name in sorted(_discovered_profiles(app))
    )


def _profile_slots_summary(cfg: Dict[str, Any]) -> str:
    slots = cfg.get("slots") or {}
    models = cfg.get("models") or {}
    pairs = []
    for level, alias in slots.items():
        entry = models.get(alias)
        if entry is None:
            continue
        pairs.append((level, f"{level} {entry.get('model') or alias}"))
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
    # Во ВХОДНОМ документе секция называется `mcp: servers:` — плоский `mcp-servers`
    # существует только в снимке flatten_app_config (config.py), а литеральный
    # верхнеуровневый `mcp-servers:` схема профиля отвергает (docs/configuration.md).
    mcp = raw.get("mcp") if isinstance(raw, dict) else None
    servers = mcp.get("servers") if isinstance(mcp, dict) else None
    if not servers:
        return ""
    return f"переключение пересоберёт MCP: {len(servers)} серверов"


def profile_value_hint(app: Any, value: str) -> str:
    """Динамический hint `/profiles` (спека §12.1): состав слотов + источник
    (проектный/глобальный) +, если целевой профиль переопределяет `mcp.servers`,
    предупреждение цены переключения. Пустое состояние — текст спеки дословно."""
    if not value:
        # Критерий пустого состояния — СУЩЕСТВОВАНИЕ профилей, не «не выбран»
        # (ревизия 2026-07-28: хинт «не настроены» при живом smoke.yml — баг смока).
        names = sorted(_discovered_profiles(app))
        if not names:
            return _PROFILE_EMPTY_HINT
        return f"не выбран · доступно: {', '.join(names)} · Enter — открыть список"
    cfg = _effective_cfg(app)
    ref = _discovered_profiles(app).get(value)
    parts = []
    summary = _profile_slots_summary(cfg)
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
_PROFILE_NONE_HINT = (
    "настройки берутся только из config.yml — файл профиля не подмешивается"
)
# Тот же литерал, что и активный маркер секции «Профили» /settings
# (`_profile_row_text`/`_none_row_text` в tui/screens/settings.py) — статус
# пикера красится тем же цветом (OK), правая колонка ArgPicker (задача 10,
# решение смока 2026-08-10).
_PROFILE_ACTIVE_STATUS = "● активен"
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
    единственную полную сборку (`probe_profile_assembly` → `load_config_ex`,
    ~6 мс), вторая берёт готовый ответ. Без этого хинт пересобирал конфиг
    заново на каждый highlight и на каждый тик поллинга главного экрана
    (`ArgPicker.desired_height`, 0.25 с) — 4 раза в секунду на UI-потоке, пока
    список просто открыт, никто не нажал ни одной клавиши."""
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
    """Опции пикера `/profiles` (спека §12.1, ревизия 2026-07-28; статус-колонка —
    задача 10, решение смока 2026-08-10): первым — «— без профиля» (сессионное
    снятие, §9.1), затем профили по алфавиту объединённого списка. 3-tuple
    (value, описание, статус): описание — источник + «(как в конфиге)» у
    профиля конфиг-указателя; статус — «● активен» ОТДЕЛЬНЫМ элементом (не
    частью описания) у ровно одной строки — той, что действует сейчас
    (`ArgPicker` красит его правой колонкой, см. `arg_picker.py`).

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
    каждый highlight И на каждый тик поллинга (`ArgPicker.desired_height`) —
    без общего кэша с этим провайдером он звал бы пробу заново 4 раза в
    секунду, пока список просто открыт."""
    refs = _discovered_profiles(app)
    effective = str(_effective_cfg(app).get("profile") or "")
    base_pointer = str(
        (getattr(app, "_config", None) or {}).get("profile") or ""
    )
    none_status = _PROFILE_ACTIVE_STATUS if not effective else ""
    options = [(PROFILE_NONE_VALUE, "работать чистым конфигом", none_status)]
    for name in sorted(refs):
        ref = refs[name]
        desc = "проектный" if ref.root_kind == "project" else "глобальный"
        if name == base_pointer:
            desc += " (как в конфиге)"
        if name == effective:
            status = _PROFILE_ACTIVE_STATUS  # действующий уже собрался — пробы не надо
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
    effective_models = _effective_cfg(app).get("models") or {}
    merged_models = {**effective_models, **(doc.get("models") or {})}
    parts = []
    # Статус строки говорит ЧТО («✗ не применится»), подсказка — ПОЧЕМУ. Причина
    # идёт ПЕРВОЙ и целиком: текст ядра самодостаточен («<путь>: …»), а рядом с
    # палитрой путь профиля не печатает никто — без него нечего чинить.
    reason = _cached_assembly_error(app, name)
    if reason:
        parts.append(reason)
    summary = _profile_slots_summary(
        {"slots": doc.get("slots") or {}, "models": merged_models}
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
# Порядок блоков читается сверху вниз: сначала «как работаем» (строгость,
# самопроверка, формат ответа), потом «чем» (уровень модели) — решение владельца.
MODES_SECTION_GROUPS: Tuple[str, ...] = ("rigor", "level")


def modes_section_names() -> list:
    """Имена строк секции «Режимы работы» в порядке показа.

    Секция НЕ равна одной группе: `level` правит глобальный `level:` конфига и
    стоит здесь же, отдельным блоком под чертой (к профилям он отношения не
    имеет — там правился чужой ключ). Знание о композиции секции живёт в одном
    месте: и рендер, и навигация, и запись, и превью меню спрашивают отсюда."""
    return [
        n for group in MODES_SECTION_GROUPS for n in group_toggle_names(group)
    ]


def effective_toggle_names(app: Any, name: str) -> Tuple[str, ...]:
    """Набор значений тоггла для текущего состояния: динамический
    (`values_provider(app)`, `/profiles`) либо статический (`spec.names`)."""
    spec = TOGGLES[name]
    if spec.values_provider is not None:
        return tuple(v.value for v in spec.values_provider(app))
    return spec.names


TOGGLES: Dict[str, ToggleSpec] = {
    "level": ToggleSpec(
        dotted="level",
        values=(
            ToggleValue("light", "", _pal.INK),
            ToggleValue("default", "", _pal.RUN_COLOR),
            ToggleValue("max", "", _pal.ACCENT),
        ),
        state=lambda cfg: str(cfg.get("level") or "default"),
        label="уровень модели",
        summary="от лёгкой до максимальной сложности задачи",
        group="level",
        settings_label="уровень модели по умолчанию",
        hint_provider=level_value_hint,
        rebuild_on_apply=False,
        palette_default=True,
    ),
    "profiles": ToggleSpec(
        dotted="profile",
        values=(),
        values_provider=profile_values,
        state=lambda cfg: str(cfg.get("profile") or ""),
        label="профили",
        summary="переключаемые наборы настроек",
        group="models",
        show_track=False,
        empty_value_text=PROFILE_NONE_VALUE,
        hint_provider=profile_value_hint,
        session_switch_profile=True,
        palette_default=True,
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
                _pal.RUN_COLOR,
            ),
            ToggleValue(
                "high",
                "самопроверка всегда, завершать только вызовом инструмента · сложные задачи, где важна полнота",
                _pal.ACCENT,
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
                _pal.RUN_COLOR,
            ),
            ToggleValue(
                "on",
                "самопроверка не даст закончить, пока запрос не закрыт · для сложных задач, где важна полнота",
                _pal.ACCENT,
            ),
        ),
        state=effective_finish_gate_mode,
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
                _pal.RUN_COLOR,
            ),
            ToggleValue(
                "deny",
                "текст не завершает ход: агент обязан закончить вызовом инструмента · для строгой автономии",
                _pal.ACCENT,
            ),
        ),
        state=effective_text_answer_mode,
        label="формат ответа",
        summary="допустим ли текст как финал хода",
        palette_default=False,
    ),
}


_HINT_KEYS_TOGGLE = "←/→/Enter — переключить в текущей сессии"
_HINT_KEYS_TOGGLE_MEDIUM = "←/→/Enter — переключить"
_HINT_KEYS_TOGGLE_SHORT = "←/→/Enter"


def _fit_hint(value: str, hint: str, keys: str, width: int) -> str:
    """Приоритетное усечение hint-строки тоггла (канон §6.1 спеки уровней
    настроек 2026-07-21, 4 ступени): 1) хвост подсказки после последнего
    ' · ' уходит первым; 2) клавиши теряют область — «переключить в текущей
    сессии» → «переключить» (область уходит ЦЕЛЫМ куском, не обрывком);
    3) клавиши сжимаются до `←/→/Enter`; 4) остаток смысла — многоточие.
    Механика (клавиши в той или иной форме) видима всегда. Settings эту
    функцию не вызывает (использует `_fit_value_hint`).

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


def _fit_value_hint(value: str, hint: str, width: int) -> str:
    """Усечение строки «<значение> — <подсказка>» БЕЗ клавишной части (канон §6,
    п.1/п.3 — п.2 «короткие клавиши» неприменим, клавиш тут нет): 1) хвост
    подсказки после последнего ' · ' уходит первым; 2) остаток — многоточие.

    Для строки внутри рамки подэкрана Settings («Режимы работы») — клавиши там
    показывает статичный футер вне рамки (см. `_fit_hint` — та же механика,
    но с клавишами внутри самой строки, там, где своего футера-подсказки нет)."""

    def build(h: str) -> str:
        return f"{value} — {h}"

    full = build(hint)
    if width <= 0 or len(full) <= width:
        return full
    core = hint.rsplit(" · ", 1)[0]
    trimmed = build(core)
    if len(trimmed) <= width:
        return trimmed
    fixed = len(build(""))
    room = max(1, width - fixed)
    return build(core[: room - 1] + "…")


def render_track(
    spec: ToggleSpec, current: str, *, extra_lit: Any = None
) -> Text:
    """Канонический трек тоггла (спека §2–§3): ● на позиции current цветом
    значения (bold), пустые позиции — ○ DEEP_DIM. `extra_lit` (спека §4.1,
    /rigor, ревизия 4): None — 4-й позиции нет вовсе (бит-в-бит как было,
    single-key тогглы); False — 4-я позиция ЗАРЕЗЕРВИРОВАНА, но пуста
    (двойной пробел + один ПРОБЕЛ вместо глифа — custom не горит, никакого
    намёка на выбираемую позицию, она больше не выбирается стрелкой вовсе);
    True — 4-я `●` BRAND (эффективная пара нестандартна — custom применена),
    при этом база вся тусклая. Только трек — колонки/подпись у потребителя."""
    idx = spec.names.index(current) if current in spec.names else 0
    t = Text()
    for i, val in enumerate(spec.values):
        if i == idx and extra_lit is not True:
            t.append("●", style=f"bold {val.color}")
        else:
            t.append("○", style=_pal.DEEP_DIM)
        if i < len(spec.values) - 1:
            t.append(" ")
    if extra_lit is not None:
        t.append("  ")  # двойной пробел — отделяет вне-шкальную позицию custom
        if extra_lit is True:
            t.append("●", style=f"bold {_pal.BRAND}")
        else:
            t.append(
                " "
            )  # резерв: 4-я позиция пуста, без тусклой ○ (ревизия 4)
    return t


def padded_track(
    spec: ToggleSpec,
    current: str,
    *,
    extra_lit: Any,
    base_w: int,
    extra_w: int,
) -> Text:
    """Трек тоггла, выровненный по ЛЕВОМУ краю общего поля (ревизия 5, спека §6:
    системный контракт, владелец здесь — переиспользуют палитра и Settings).
    `render_track` остаётся ядром без изменений — эта функция только паддит
    его результат пробелами СПРАВА (не слева, как было): базовый трек — до
    `base_w` (тройки друг под другом по левому краю), затем extra-зона
    (двойной пробел + глиф/пробел custom-резерва, если `extra_w` > 0) — до
    `extra_w`, пустая целиком у строк без своего резерва (`extra_lit=None`,
    дети пресета) — место общее для всех видимых строк подвала."""
    base_len = 2 * len(spec.values) - 1
    full = render_track(spec, current, extra_lit=extra_lit)
    t = Text()
    t.append_text(full[:base_len])
    t.append(" " * (base_w - base_len))
    if extra_w:
        extra_part = full[base_len:]
        t.append_text(extra_part)
        t.append(" " * (extra_w - len(extra_part.plain)))
    return t


def toggle_state(app: Any, name: str) -> str:
    """Текущее эффективное значение тоггла `name` (`gate` — через правило
    коллизии, `answers` — сырое значение). Источник — эффективный конфиг
    (база + сессионные переопределения), не `app._config`."""
    getter = getattr(app, "effective_config", None)
    cfg = (
        getter() if callable(getter) else (getattr(app, "_config", None) or {})
    )
    return TOGGLES[name].state(cfg)


def toggle_state_base(app: Any, name: str) -> str:
    """Глобальное (файловое) значение тоггла `name`: из `app._config` БЕЗ
    сессионных переопределений — то, что правит секция Settings (в отличие от
    `toggle_state`, отдающего эффективное = база + сессия). Читается тем же
    правилом коллизии (`TOGGLES[name].state`), что и эффективное, — иначе
    строка секции и оверрайд-пометка разошлись бы правилами."""
    base = getattr(app, "_config", None) or {}
    return TOGGLES[name].state(base)


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


def cycle_value(name: str, current: str, direction: int) -> str:
    """Следующее значение тоггла `name` по кругу (±1 по TOGGLES[name].names). Чистая
    функция — ничего не пишет и не применяет; роутинг main.py (←/→/Enter на toggle-
    строке) зовёт её от ТЕКУЩЕГО отображаемого состояния (`toggle_state`) прямо перед
    `apply_toggle` — мгновенная модель, черновика нет."""
    names = TOGGLES[name].names
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

    `/profiles` (`session_switch_profile`, спека §9.1) — применяет через
    `app.switch_session_profile` (пересборка + notice + сброс предохранителя —
    отдельная семантика, не generic single-key путь). `/level`
    (`rebuild_on_apply=False`, спека §6.4) — пишет сессионный оверрайд, но
    единственный НЕ зовёт `invalidate_runner`: уровень читается инжектируемым
    провайдером на старте хода, пересборка executor ему не нужна."""
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
    if spec.rebuild_on_apply:
        app.invalidate_runner()
    return toggle_state(app, name)


def _global_cfg(app: Any) -> Dict[str, Any]:
    return getattr(app, "_config", None) or {}


def _effective_cfg(app: Any) -> Dict[str, Any]:
    getter = getattr(app, "effective_config", None)
    return getter() if callable(getter) else _global_cfg(app)


def next_toggle_value(app: Any, name: str, direction: int) -> str:
    """Generic следующий шаг тоггла по кругу — та же формула, что у
    `cycle_value`, но отправная точка читается через эффективное состояние
    (`toggle_state(app, name)`), а не передаётся аргументом: preset-тогглу
    нужен резолв пары через конфиг приложения, single-key — просто текущее
    значение. Custom НЕ позиция трека (ревизия 4) — эффективное значение вне
    `spec.names` (отображаемый custom) трактуется как idx 0, шаг с него по
    кругу уводит на пресет; custom этой функцией никогда не возвращается.

    `/profiles` (спека §12.1) — набор значений динамический
    (`effective_toggle_names`, а не статический `TOGGLES[name].names`); пустой
    набор (профили не настроены) — цикл no-op, возвращает текущее значение
    (пустую строку)."""
    names = effective_toggle_names(app, name)
    current = toggle_state(app, name)
    if not names:
        return current
    idx = names.index(current) if current in names else 0
    return names[(idx + direction) % len(names)]


def preset_track_extra(app: Any, name: str) -> Any:
    """Состояние 4-й позиции трека /rigor (ревизия 3, спека §2, §4.1): для
    preset-тоггла ВСЕГДА не-None (позиция custom видна всегда) — `True` ⇔
    эффективная пара нестандартна (custom применена), иначе `False`. Глобал
    больше не участвует (был отдельный «постоянный» случай — упразднён).
    single-key → `None` (позиции нет вовсе)."""
    spec = TOGGLES[name]
    if not spec.preset_keys:
        return None
    return spec.state(_effective_cfg(app)) == PRESET_CUSTOM


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
    cfg = _global_cfg(app) if scope == "global" else _effective_cfg(app)
    x = SESSION_KEYS[spec.preset_keys[0]].effective_value(cfg)
    y = SESSION_KEYS[spec.preset_keys[1]].effective_value(cfg)
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
