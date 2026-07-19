"""Тогглы командной палитры: владелец состояния и переключения.

Конфиг — единственный источник истины; палитра — витрина. Правило коллизии
finish_gate_mode со старым глобальным рубильником — спека
2026-07-16-palette-toggles-gate-answers-design §3."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Tuple

from rich.text import Text

from tui import palette as _pal


def effective_finish_gate_mode(cfg: Dict[str, Any]) -> str:
    """Эффективный режим /gate: явный tui-ключ > явный глобальный off > дефолт on."""
    mode = str(cfg.get("tui_finish_gate_mode") or "on")
    if bool(cfg.get("tui_finish_gate_mode_explicit")):
        return mode
    if bool(cfg.get("strict_turn_finish_explicit_false")):
        return "off"
    return "on"


def apply_finish_gate_mode(cfg: Dict[str, Any]) -> None:
    """TUI-wiring: спроецировать эффективный режим /gate в runtime-ключ строгости.

    Зовётся ТОЛЬКО из TUI-сборки конфига (паттерн text_answer_mode): CLI/server/swarm
    не зовут — у них глобальный agent.strict_turn_finish_validation как был."""
    cfg["strict_turn_finish_validation"] = effective_finish_gate_mode(cfg) == "on"


def _answers_state(cfg: Dict[str, Any]) -> str:
    """Сырое (не эффективное — коллизии нет) значение /answers из flatten-конфига."""
    return str(cfg.get("tui_text_answer_mode") or "judge")


@dataclass(frozen=True)
class ToggleValue:
    """Одно значение тоггла: каноническое имя + подсказка (§5 спеки) + цвет ползунка
    (§3 спеки; токен-константа tui/palette.py)."""

    value: str
    hint: str
    color: str


@dataclass(frozen=True)
class ToggleSpec:
    """Спецификация тоггла палитры: куда писать + допустимые значения по кругу (позиции
    трека слева направо) + читатель состояния + категория как данные (спека
    2026-07-18-settings-work-modes-design.md §4): `label` — имя тоггла (строки
    подэкрана Settings, пары превью), `summary` — суть для описания в палитре
    (`f"{label}: {summary}"`, генерится в commands.build_default_registry)."""

    dotted: str
    values: Tuple[ToggleValue, ...]
    state: Callable[[Dict[str, Any]], str]
    label: str
    summary: str

    @property
    def names(self) -> Tuple[str, ...]:
        return tuple(v.value for v in self.values)


TOGGLES: Dict[str, ToggleSpec] = {
    "gate": ToggleSpec(
        dotted="tui.finish_gate_mode",
        values=(
            ToggleValue(
                "off",
                "самопроверка выключена · быстрее: для простых задач и экономии токенов",
                _pal.INK,
            ),
            ToggleValue(
                "on",
                "самопроверка не даст закончить, пока запрос не закрыт · для сложных задач, где важна полнота",
                _pal.ACCENT,
            ),
        ),
        state=effective_finish_gate_mode,
        label="самопроверка выполнения задачи",
        summary="закрыт ли запрос пользователя",
    ),
    "answers": ToggleSpec(
        dotted="tui.text_answer_mode",
        values=(
            ToggleValue(
                "deny",
                "текст не завершает ход: агент обязан закончить вызовом инструмента · для строгой автономии",
                _pal.INK,
            ),
            ToggleValue(
                "judge",
                "текстовый ответ принимается, если проходит самопроверку · универсальный режим",
                _pal.RUN_COLOR,
            ),
            ToggleValue(
                "allow",
                "текстовый ответ всегда принимается · для чата и быстрых ответов",
                _pal.ACCENT,
            ),
        ),
        state=_answers_state,
        label="формат ответа",
        summary="допустим ли текст как финал хода",
    ),
}


_HINT_KEYS_TOGGLE = "←/→/Enter — переключить"
_HINT_KEYS_TOGGLE_SHORT = "←/→/Enter"


def _fit_hint(value: str, hint: str, keys: str, width: int) -> str:
    """Приоритетное усечение hint-строки тоггла (канон слайдеров §6): 1) хвост подсказки
    после последнего ' · ' уходит первым; 2) клавиши сжимаются до `←/→/Enter`;
    3) остаток механики — многоточие. Механика видима всегда.

    Владелец канонического представления тоггла (рядом с render_track) — переиспользуется
    и палитрой (command_palette.py), и подэкраном Settings (screens/settings.py)."""
    def build(h: str, k: str) -> str:
        return f"{value} — {h}  ·  {k}"

    if width <= 0 or len(build(hint, keys)) <= width:
        return build(hint, keys)
    core = hint.rsplit(" · ", 1)[0]                       # 1) без «кому подходит»
    if len(build(core, keys)) <= width:
        return build(core, keys)
    if len(build(core, _HINT_KEYS_TOGGLE_SHORT)) <= width:  # 2) короткие клавиши
        return build(core, _HINT_KEYS_TOGGLE_SHORT)
    fixed = len(build("", _HINT_KEYS_TOGGLE_SHORT))
    room = max(1, width - fixed)
    return build(core[: room - 1] + "…", _HINT_KEYS_TOGGLE_SHORT)  # 3) многоточие


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


def render_track(spec: ToggleSpec, current: str) -> Text:
    """Канонический трек тоггла (спека §2–§3): ● на позиции current цветом
    значения (bold), остальные позиции — незакрашенные ○ DEEP_DIM.
    Только трек — колонки/подпись у потребителя (палитра, Settings)."""
    idx = spec.names.index(current) if current in spec.names else 0
    t = Text()
    for i, val in enumerate(spec.values):
        if i == idx:
            t.append("●", style=f"bold {val.color}")
        else:
            t.append("○", style=_pal.DEEP_DIM)
        if i < len(spec.values) - 1:
            t.append(" ")
    return t


def toggle_state(app: Any, name: str) -> str:
    """Текущее эффективное значение тоггла `name` (`gate` — через правило коллизии,
    `answers` — сырое значение конфига)."""
    cfg = getattr(app, "_config", None) or {}
    return TOGGLES[name].state(cfg)


def cycle_value(name: str, current: str, direction: int) -> str:
    """Следующее значение тоггла `name` по кругу (±1 по TOGGLES[name].names). Чистая
    функция — ничего не пишет и не применяет; роутинг main.py (←/→/Enter на toggle-
    строке) зовёт её от ТЕКУЩЕГО отображаемого состояния (`toggle_state`) прямо перед
    `apply_toggle` — мгновенная модель, черновика нет."""
    names = TOGGLES[name].names
    idx = names.index(current) if current in names else 0
    return names[(idx + direction) % len(names)]


def apply_toggle(app: Any, name: str, value: str) -> str:
    """Применить ЯВНО переданное `value` тоггла `name`: записать в config.yml (тем же
    путём, что /settings — write_values + шаблон-фолбэк для отсутствующего ключа +
    схемная валидация) и hot-reload'нуть app. Возвращает новое эффективное значение.

    Мгновенная модель (черновика нет): вызывающий (роутинг main.py) заранее считает
    `value` через `cycle_value` от текущего отображаемого состояния и передаёт сюда
    готовое значение — пишем ровно то, что показано на экране, apply_toggle сам
    направление не пересчитывает.

    `value` обязан быть одним из `TOGGLES[name].names` — иначе `ValueError` (рассинхрон
    палитры и тоггла — программная ошибка, а не штатный ввод, поэтому падаем, а не
    молча no-op).

    Ошибки записи (ValueError/OSError) не гасятся — летят наружу, вызывающий (роутинг
    main.py) показывает флеш «✗ не сохранено: …» и оставляет палитру открытой."""
    import onboarding_config_writer as w
    from config import _default_config_path, validate_config_text

    spec = TOGGLES[name]
    if value not in spec.names:
        raise ValueError(f"invalid value {value!r} for toggle {name!r}, expected one of {spec.names}")
    w.write_values(
        app._config_path,
        {spec.dotted: value},
        template_path=_default_config_path(),
        validate=validate_config_text,
    )
    app.reload_config()
    return toggle_state(app, name)
