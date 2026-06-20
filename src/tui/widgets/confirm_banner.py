"""ConfirmBanner — служебное подтверждение, sticky-блок над полем ввода.

Отличается от AskHumanBanner назначением (security/служебный confirm, не вопрос
агента по задаче) и набором вариантов. Трейс остаётся виден; поле ввода прячет
вызывающая сторона (см. wiring)."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static


class ConfirmBanner(Vertical):
    DEFAULT_CSS = ""  # стили в styles.tcss (.confirm-banner)

    def __init__(self, *, title: str, question: str, **kwargs) -> None:
        kwargs.setdefault("classes", "")
        kwargs["classes"] = ("confirm-banner " + kwargs["classes"]).strip()
        super().__init__(**kwargs)
        self.title = title
        self.question = question

    def compose(self) -> ComposeResult:
        yield Static(f"? {self.title}", classes="cb-label", markup=False)
        yield Static(self.question, classes="cb-question", markup=False)
        yield Static("[Y] да   [N] нет", classes="cb-options", markup=False)
