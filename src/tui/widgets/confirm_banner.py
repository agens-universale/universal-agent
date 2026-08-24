"""ConfirmBanner — служебное подтверждение, sticky-блок над полем ввода.

Отличается от AskHumanBanner назначением (security/служебный confirm, не вопрос
агента по задаче) и набором вариантов. Трейс остаётся виден; поле ввода прячет
вызывающая сторона (см. wiring).

fields — структурированные детали решения (Tool/Path/Command/Reason/Risk),
которые приходят из confirm-порта (request_confirm). Рендерятся строками между
вопросом и вариантами — чтобы баннер был информативен (CLI-промпт их печатает,
здесь — тоже)."""
from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from tui.palette import ACCENT, BG, BRIGHT, DIM, RUN_COLOR

# Глиф защитного шлюза (caution). НЕ "?" — это не вопрос агента, а security-gate.
# Width-safe-кандидат; если ⚠ в целевом терминале рендерится шире 1 ячейки —
# заменить на "▲" или "!" (единственная точка правки).
GUARD_GLYPH = "⚠"


def build_guard_label(title: str, via: str | None = None) -> Text:
    """Заголовок шлюза: янтарный глиф ⚠ + bright-титул. '?' остаётся только
    в самой строке вопроса ('Разрешить …?'), не в заголовке.
    via — метка субагента (child HITL), например `via subagent_worker`."""
    t = Text()
    t.append(f"{GUARD_GLYPH}  ", style=RUN_COLOR)  # двойной пробел: ⚠ широкий, одиночный «съедается»
    label = f"{title} · via {via}" if via else title
    t.append(label, style=BRIGHT)
    return t


# Варианты 2×2: (keycap, label, choice_id).
# 0 — Y allow_remember, 1 — O allow_once, 2 — N deny_remember, 3 — T deny_once.
# «И подобные в сессии» = soft signal судье через insession_examples (не hard allow/deny класса).
_OPTIONS = [
    ("[Y]", "Разрешить команду и подобные в сессии", "allow_remember"),
    ("[O]", "Разрешить разово", "allow_once"),
    ("[N]", "Запретить команду и подобные в сессии", "deny_remember"),
    ("[T]", "Запретить разово", "deny_once"),
]

# Метки primary payload-поля — рендерится первым, multiline, cb-payload class.
_PAYLOAD_LABELS = frozenset({
    "команда", "код", "command", "code", "path", "url", "путь",
})


def _is_payload_label(label: str) -> bool:
    return str(label).strip().casefold() in _PAYLOAD_LABELS


def _fmt_payload_value(value: object, *, limit: int = 500) -> str:
    """Multiline payload: newlines preserved; over limit → head + ``… (+N chars)``."""
    if isinstance(value, (list, tuple)):
        text = "\n".join(str(x) for x in value)
    else:
        text = str(value)
    text = text.strip()
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return f"{text[:limit]}\n… (+{omitted} chars)"


def _fmt_field_value(value: object, *, limit: int = 200) -> str:
    """Однострочное компактное значение мета-поля: списки — через запятую,
    переводы строк схлопнуты, длинное — усечено."""
    if isinstance(value, (list, tuple)):
        text = ", ".join(str(x) for x in value)
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _render_options(selected: int) -> Text:
    """Вертикальный рендер вариантов; выбранный — teal-«кнопка», прочие — keycap accent + текст dim."""
    t = Text()
    for idx, (key, label, _choice) in enumerate(_OPTIONS):
        if idx > 0:
            t.append("\n")
        prefix = "▸ " if idx == selected else "  "
        if idx == selected:
            t.append(f"{prefix}{key} {label} ", style=f"bold {BG} on {ACCENT}")
        else:
            t.append(prefix)
            t.append(f"{key}", style=ACCENT)
            t.append(f" {label}", style=DIM)
    return t


def choice_for(selected: int) -> str:
    """Маппинг индекса выделенного варианта → строка выбора (4-way)."""
    if 0 <= selected < len(_OPTIONS):
        return _OPTIONS[selected][2]
    return "deny_remember"


class ConfirmBanner(Vertical):
    DEFAULT_CSS = ""  # стили в styles.tcss (.confirm-banner)
    # Фокус на баннере, пока висит confirm: иначе Tab/клик по ToolCallRow
    # перехватывает Enter (открывает detail) до Screen.on_key.
    can_focus = True

    def __init__(
        self,
        *,
        banner_title: str,
        question: str,
        fields: list | None = None,
        via: str | None = None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("classes", "")
        kwargs["classes"] = ("confirm-banner " + kwargs["classes"]).strip()
        super().__init__(**kwargs)
        self.banner_title = banner_title
        self.question = question
        self.fields = list(fields or [])
        self.via = via
        self.selected: int = 0  # 0 = Y/affirmative по умолчанию
        self._options_static: Static | None = None

    def compose(self) -> ComposeResult:
        yield Static(
            build_guard_label(self.banner_title, via=self.via),
            classes="cb-label",
            markup=False,
        )
        yield Static(self.question, classes="cb-question", markup=False)
        for label, value in self.fields:
            if _is_payload_label(label):
                text = _fmt_payload_value(value)
                css_class = "cb-payload"
            else:
                text = _fmt_field_value(value)
                css_class = "cb-field"
            if not text:
                continue  # пустые поля (напр. Reason="") не засоряют баннер
            yield Static(f"  {label}: {text}", classes=css_class, markup=False)
        yield Static("", classes="cb-spacer")  # пустая строка отделяет инфо от вариантов
        opts = Static(_render_options(self.selected), classes="cb-options")
        self._options_static = opts
        yield opts

    def move_selection(self, delta: int) -> None:
        """Сдвинуть выделение на delta (-1 = вверх, +1 = вниз)."""
        self.selected = max(0, min(len(_OPTIONS) - 1, self.selected + delta))
        if self._options_static is not None and self._options_static.is_mounted:
            self._options_static.update(_render_options(self.selected))
