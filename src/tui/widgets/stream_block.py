"""Ephemeral in-progress streaming block (TUI). Shows live model output for the
current step; removed when the canonical thread row arrives or the run ends."""
from __future__ import annotations

from textual.widgets import Static


class StreamBlock(Static):
    DEFAULT_CSS = ""  # styled via .stream-block in styles.tcss

    def __init__(self) -> None:
        super().__init__("", classes="stream-block", markup=False)

    def set_text(self, text: str, *, thinking: bool = False) -> None:
        if not self.is_mounted:
            return
        if thinking:
            self.add_class("stream-block-think")
        else:
            self.remove_class("stream-block-think")
        self.update(text)
