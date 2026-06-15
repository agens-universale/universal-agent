"""Ephemeral in-progress streaming block (TUI). Shows live model output for the
current step; removed when the canonical thread row arrives or the run ends."""
from __future__ import annotations

from textual.widgets import Static


class StreamBlock(Static):
    DEFAULT_CSS = ""  # styled via .stream-block in styles.tcss

    def __init__(self) -> None:
        super().__init__("", classes="stream-block", markup=False)

    def set_text(self, text: str) -> None:
        if self.is_mounted:
            self.update(text)
