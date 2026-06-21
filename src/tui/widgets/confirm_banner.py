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

from tui.palette import ACCENT, BG, DIM

# Варианты: (keycap, label). 0 — once, 1 — session, 2 — deny.
_OPTIONS = [
    ("[Y]", "Разрешить"),
    ("[S]", "Разрешить в этой сессии — не спрашивать снова"),
    ("[N]", "Нет — отклонить"),
]


def _fmt_field_value(value: object, *, limit: int = 200) -> str:
    """Однострочное компактное значение поля: списки — через запятую,
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
    for idx, (key, label) in enumerate(_OPTIONS):
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
    """Маппинг индекса выделенного варианта → строка выбора."""
    return ("once", "session", "deny")[selected] if 0 <= selected < 3 else "deny"


class ConfirmBanner(Vertical):
    DEFAULT_CSS = ""  # стили в styles.tcss (.confirm-banner)

    def __init__(
        self,
        *,
        banner_title: str,
        question: str,
        fields: list | None = None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("classes", "")
        kwargs["classes"] = ("confirm-banner " + kwargs["classes"]).strip()
        super().__init__(**kwargs)
        self.banner_title = banner_title
        self.question = question
        self.fields = list(fields or [])
        self.selected: int = 0  # 0 = Y/affirmative по умолчанию
        self._options_static: Static | None = None

    def compose(self) -> ComposeResult:
        yield Static(f"? {self.banner_title}", classes="cb-label", markup=False)
        yield Static(self.question, classes="cb-question", markup=False)
        for label, value in self.fields:
            text = _fmt_field_value(value)
            if not text:
                continue  # пустые поля (напр. Reason="") не засоряют баннер
            yield Static(f"  {label}: {text}", classes="cb-field", markup=False)
        opts = Static(_render_options(self.selected), classes="cb-options")
        self._options_static = opts
        yield opts

    def move_selection(self, delta: int) -> None:
        """Сдвинуть выделение на delta (-1 = вверх, +1 = вниз)."""
        self.selected = max(0, min(len(_OPTIONS) - 1, self.selected + delta))
        if self._options_static is not None and self._options_static.is_mounted:
            self._options_static.update(_render_options(self.selected))
