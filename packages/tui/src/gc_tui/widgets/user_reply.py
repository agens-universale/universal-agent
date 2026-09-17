"""UserReplyContainer — реплика пользователя с band-фоном и 2px teal кромкой слева."""
from __future__ import annotations

from typing import Literal

from rich.text import Text
from typing import Optional

from textual.message import Message
from textual.widget import Widget

from gc_tui.widgets.click_model import ClickModelMixin

from gc_tui.palette import ERR as _ERR_RED


_State = Literal["normal", "pending", "failed"]


class UserReplyContainer(ClickModelMixin, Widget):
    """Контейнер реплики пользователя.

    Визуал: фон `$band-bg`, 2px кромка `$accent` слева на всю высоту хода
    (стили — `.user-band` в `src/tui/styles.tcss`, §2.6 спеки).

    Состояния (spec 2026-05-26):
    - `normal`  — обычная (зелёная) кромка, без маркеров.
    - `pending` — приглушённая teal-кромка (`.user-reply-pending`), без маркеров.
    - `failed`  — красная кромка (`.user-reply-failed`) + инлайн `[!] ` перед `> `.

    Маркер `> ` — только на первой строке; продолжение многострочной реплики
    выравнивается без повтора маркера (blockquote-стиль).
    """

    DEFAULT_CSS = ""  # стили — .user-band / .user-reply-pending / .user-reply-failed в styles.tcss
    can_focus = True

    def __init__(
        self,
        text: str,
        *,
        event_no: int = 0,
        state: _State = "normal",
        **kwargs,
    ) -> None:
        classes = kwargs.pop("classes", "")
        merged = f"user-band {classes}".strip()
        if state == "pending":
            merged += " user-reply-pending"
        elif state == "failed":
            merged += " user-reply-failed"
        super().__init__(classes=merged, **kwargs)
        self._text = text
        self._event_no = event_no
        self._state: _State = state

    @property
    def state(self) -> _State:
        return self._state

    def set_state(self, new_state: _State) -> None:
        """Переключить визуальное состояние реплики."""
        self.remove_class("user-reply-pending")
        self.remove_class("user-reply-failed")
        if new_state == "pending":
            self.add_class("user-reply-pending")
        elif new_state == "failed":
            self.add_class("user-reply-failed")
        self._state = new_state
        self.refresh()

    def render(self) -> Text:
        lines = self._text.split("\n")

        text = Text()
        for i, line in enumerate(lines):
            if i > 0:
                text.append("\n")
            if i == 0:
                if self._state == "failed":
                    text.append("[!] ", style=_ERR_RED)
                text.append(f"> {line}")
            else:
                # продолжение многострочной реплики выравнивается под текстом
                # после маркера "> " (2 пробела).
                text.append(f"  {line}")
        return text

    def _open_request(self) -> Optional[Message]:
        if not self._event_no:
            return None
        from gc_tui.messages import OpenEventRequest
        return OpenEventRequest(self._event_no)
