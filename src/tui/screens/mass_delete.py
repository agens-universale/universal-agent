"""MassDeleteScreen — модалка массового удаления сессий по критерию.

Режимы (↑/↓): «оставить последние N» / «удалить старше N дней».
Enter (при count > 0) → dismiss({"keep_last": N}) | dismiss({"older_than_days": N}).
Esc → dismiss(None). Живой счётчик — через select_for_deletion.
Образец — RenameScreen/ConfirmScreen (инлайновый DEFAULT_CSS)."""
from __future__ import annotations

from typing import Optional

from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from tui.palette import ACCENT, BRIGHT, DIM, INK
from tui.services.session_delete_select import select_for_deletion
from utils.is_number import is_number

_MODES = ("keep_last", "older_than_days")
_MODE_LABEL = {
    "keep_last": "оставить последние N",
    "older_than_days": "удалить старше N дней",
}


class MassDeleteScreen(ModalScreen[Optional[dict]]):
    DEFAULT_CSS = f"""
    MassDeleteScreen {{ align: center middle; }}
    #mass-dialog {{
        background: #161A23;
        border: round {ACCENT};
        border-title-color: {BRIGHT};
        border-title-style: bold;
        padding: 1 2;
        width: 60;
        height: auto;
    }}
    #mass-modes {{ color: {INK}; }}
    #mass-n {{ border: none; background: #0E1119; color: {INK}; }}
    #mass-count {{ color: {ACCENT}; margin-top: 1; }}
    #mass-hint {{ color: {DIM}; margin-top: 1; }}
    """

    def __init__(self, *, tasks: list, now) -> None:
        super().__init__()
        self._tasks = tasks
        self._now = now
        self._mode_idx = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="mass-dialog"):
            yield Static(self._modes_text(), id="mass-modes", markup=False)
            yield Input(value="20", id="mass-n")
            yield Static("", id="mass-count", markup=False)
            yield Static("[↑↓] режим · [Enter] далее · [Esc] отмена",
                         id="mass-hint", markup=False)

    def on_mount(self) -> None:
        self.query_one("#mass-dialog").border_title = "Массовое удаление"
        self.query_one("#mass-n", Input).focus()
        self._refresh()

    # ---- helpers ----
    def _mode(self) -> str:
        return _MODES[self._mode_idx]

    def _modes_text(self) -> str:
        return "   ".join(
            f"({'•' if i == self._mode_idx else ' '}) {_MODE_LABEL[m]}"
            for i, m in enumerate(_MODES)
        )

    def _n(self) -> Optional[int]:
        raw = self.query_one("#mass-n", Input).value.strip()
        return int(raw) if is_number(raw) else None

    def _ids(self) -> list:
        n = self._n()
        if n is None:
            return []
        return select_for_deletion(self._tasks, now=self._now, **{self._mode(): n})

    def _refresh(self) -> None:
        self.query_one("#mass-modes", Static).update(self._modes_text())
        self.query_one("#mass-count", Static).update(
            f"→ под удаление: {len(self._ids())} из {len(self._tasks)}")

    # ---- events ----
    def on_input_changed(self, event: Input.Changed) -> None:
        self._refresh()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        n = self._n()
        if n is not None and self._ids():
            self.dismiss({self._mode(): n})
        event.stop()

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss(None)
            event.stop()
        elif event.key in ("up", "down"):
            self._mode_idx = (self._mode_idx + 1) % len(_MODES)
            self._refresh()
            event.stop()
