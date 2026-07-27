"""Тогглы командной палитры: витрина и переключение.

Конфиг — единственный источник истины; палитра — витрина. Действующее значение
ключа считает его владелец (tui/session_settings.py) — здесь только читаем, чтобы
витрина и сравнение с умолчанием не разошлись правилами."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from rich.text import Text

from tui import palette as _pal
from tui.session_settings import (
    SESSION_KEYS,
    effective_finish_gate_mode,
    effective_text_answer_mode,
)


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


# --- /rigor: пресет как витрина над двумя ключами (спека §2, §6) --------------
# Пресет НЕ хранится (производная). Пара — те же два существующих ключа сессии/
# глобала; SESSION_KEYS их не расширяет — /rigor лишь ставит и резолвит.
RIGOR_KEYS = ("tui.finish_gate_mode", "tui.text_answer_mode")
PRESET_CUSTOM = "custom"

PRESETS: Dict[str, Dict[str, str]] = {
    "low":    {"tui.finish_gate_mode": "off",   "tui.text_answer_mode": "allow"},
    "medium": {"tui.finish_gate_mode": "smart", "tui.text_answer_mode": "judge"},
    "high":   {"tui.finish_gate_mode": "on",    "tui.text_answer_mode": "deny"},
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
    от резолвера, применение раскладывается в пару; спека §6)."""

    values: Tuple[ToggleValue, ...]
    state: Callable[[Dict[str, Any]], str]
    label: str
    summary: str
    dotted: str = ""
    palette_default: bool = True
    preset_keys: Tuple[str, ...] = ()

    @property
    def names(self) -> Tuple[str, ...]:
        return tuple(v.value for v in self.values)


TOGGLES: Dict[str, ToggleSpec] = {
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
        dotted="tui.finish_gate_mode",
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
        dotted="tui.text_answer_mode",
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
    core = hint.rsplit(" · ", 1)[0]                       # 1) без «кому подходит»
    if len(build(core, keys)) <= width:
        return build(core, keys)
    if len(build(core, _HINT_KEYS_TOGGLE_MEDIUM)) <= width:  # 2) без области
        return build(core, _HINT_KEYS_TOGGLE_MEDIUM)
    if len(build(core, _HINT_KEYS_TOGGLE_SHORT)) <= width:  # 3) короткие клавиши
        return build(core, _HINT_KEYS_TOGGLE_SHORT)
    fixed = len(build("", _HINT_KEYS_TOGGLE_SHORT))
    room = max(1, width - fixed)
    return build(core[: room - 1] + "…", _HINT_KEYS_TOGGLE_SHORT)  # 4) многоточие


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


def render_track(spec: ToggleSpec, current: str, *, extra_lit: Any = None) -> Text:
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
        t.append("  ")   # двойной пробел — отделяет вне-шкальную позицию custom
        if extra_lit is True:
            t.append("●", style=f"bold {_pal.BRAND}")
        else:
            t.append(" ")   # резерв: 4-я позиция пуста, без тусклой ○ (ревизия 4)
    return t


def padded_track(
    spec: ToggleSpec, current: str, *, extra_lit: Any, base_w: int, extra_w: int
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
    cfg = getter() if callable(getter) else (getattr(app, "_config", None) or {})
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
    Откатывается целиком при OSError; вызывающий показывает «✗ не сохранено»."""
    spec = TOGGLES[name]
    if spec.preset_keys:
        if value not in PRESETS:
            raise ValueError(
                f"invalid preset value {value!r} for toggle {name!r}, "
                f"expected one of {tuple(PRESETS)}"
            )
        app.apply_session_overrides(PRESETS[value])
        app.invalidate_runner()
        return toggle_state(app, name)
    if value not in spec.names:
        raise ValueError(
            f"invalid value {value!r} for toggle {name!r}, expected one of {spec.names}"
        )
    app.apply_session_override(spec.dotted, value)
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
    кругу уводит на пресет; custom этой функцией никогда не возвращается."""
    names = TOGGLES[name].names
    current = toggle_state(app, name)
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


def preset_custom_hint(app: Any, name: str, *, scope: str = "effective") -> str:
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
