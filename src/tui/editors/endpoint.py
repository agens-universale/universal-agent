"""Редактор адреса API-сервера: hint + PasteInput + структурная валидация."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, Static

from tui.widgets.paste_input import PasteInput

_HINT = "напр. https://host/v1/chat/completions"
_ERROR = "нужны http(s):// и хост"


class EndpointEditor(Vertical):
    def __init__(self, *, current: str, input_id: str = "onb-endpoint",
                 framed: bool = False) -> None:
        super().__init__(classes="onb-dyn")
        self._current = current or ""
        self._input_id = input_id
        self._framed = framed

    def compose(self) -> ComposeResult:
        cls = "onb-dyn set-framed" if self._framed else "onb-dyn"
        yield PasteInput(value=self._current, placeholder=_HINT,
                         id=self._input_id, classes=cls)

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def focus_input(self) -> None:
        self.query_one(Input).focus()

    def submit(self) -> tuple[bool, str | None]:
        """(ok, url|None). Пусто = None («оставить как есть»). Невалидно →
        (False, None), ошибка показана внутри редактора."""
        from onboarding import valid_endpoint_url
        raw = self.query_one(Input).value.strip()
        for e in self.query("#onb-error"):
            e.remove()
        if not raw:
            return True, None
        if not valid_endpoint_url(raw):
            self.mount(Static(_ERROR, id="onb-error", classes="onb-warn onb-dyn"))
            return False, None
        return True, raw
