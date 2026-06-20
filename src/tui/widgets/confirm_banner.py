"""ConfirmBanner — служебное подтверждение, sticky-блок над полем ввода.

Отличается от AskHumanBanner назначением (security/служебный confirm, не вопрос
агента по задаче) и набором вариантов. Трейс остаётся виден; поле ввода прячет
вызывающая сторона (см. wiring).

fields — структурированные детали решения (Tool/Path/Command/Reason/Risk),
которые приходят из confirm-порта (request_confirm). Рендерятся строками между
вопросом и вариантами — чтобы баннер был информативен (CLI-промпт их печатает,
здесь — тоже)."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static


def _fmt_field_value(value: object, *, limit: int = 200) -> str:
    """Однострочное компактное значение поля: списки — через запятую,
    переводы строк схлопнуты, длинное — усечено."""
    if isinstance(value, (list, tuple)):
        text = ", ".join(str(x) for x in value)
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


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

    def compose(self) -> ComposeResult:
        yield Static(f"? {self.banner_title}", classes="cb-label", markup=False)
        yield Static(self.question, classes="cb-question", markup=False)
        for label, value in self.fields:
            text = _fmt_field_value(value)
            if not text:
                continue  # пустые поля (напр. Reason="") не засоряют баннер
            yield Static(f"  {label}: {text}", classes="cb-field", markup=False)
        yield Static("[Y] да   [N] нет", classes="cb-options", markup=False)
