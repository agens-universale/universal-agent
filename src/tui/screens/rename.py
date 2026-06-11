"""RenameScreen — модалка переименования сессии (заголовок в tui_meta.title).

Enter → dismiss(trimmed str) (пустая строка = запрос отката к промпту).
Esc → dismiss(None) (отмена). Образец — ConfirmScreen."""
from __future__ import annotations

from typing import Optional

from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from tui.palette import ACCENT, BRIGHT, DIM, INK


class RenameScreen(ModalScreen[Optional[str]]):
    DEFAULT_CSS = f"""
    RenameScreen {{ align: center middle; }}
    #rename-dialog {{
        background: #161A23;
        border: round {ACCENT};
        border-title-color: {BRIGHT};
        border-title-style: bold;
        padding: 1 2;
        width: 64;
        height: auto;
    }}
    #rename-input {{ border: none; background: #0E1119; color: {INK}; }}
    #rename-hint {{ color: {DIM}; margin-top: 1; }}
    """

    def __init__(self, *, current: str) -> None:
        super().__init__()
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="rename-dialog"):
            yield Input(value=self._current, id="rename-input")
            yield Static("Enter сохранить · Esc отмена", id="rename-hint", markup=False)

    def on_mount(self) -> None:
        self.query_one("#rename-dialog").border_title = "Переименование сессии"
        self.query_one("#rename-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())
        event.stop()

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss(None)
            event.stop()
