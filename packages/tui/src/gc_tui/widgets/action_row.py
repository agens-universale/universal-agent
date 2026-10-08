"""Строка-действие формы: позиция курсора, открывающая отдельный экран.

Канон: «▸ <что это>   <клавиши> — <действие>». Указатель `▸` стоит всегда: он
говорит, что строка куда-то ведёт, ещё до того как на неё встали, и держит текст
на месте. Вне курсора он приглушён, под курсором — акцентный, и строка с фоном,
как пункт меню настроек. Строки, меняющие значение на месте (галка, селектор),
указателя не несут. Курсор ведёт хозяин-форма и сообщает о нём через
`set_selected`; клик и Enter на строке приходят хозяину экрана одним сообщением
`Pressed` — тем же путём, что и своя F-клавиша строки.

Неактивная строка (`set_content(..., enabled=False)`) стоит на том же месте, но
вести ей некуда: вся приглушена, указателя и клавиш нет, клик молчит. Текст её
говорит почему. Отступ под указатель сохраняется — текст не прыгает при смене
состояния.
"""

from __future__ import annotations

from rich.text import Text
from textual.message import Message
from textual.widgets import Static

from gc_tui.palette import ACCENT, DEEP_DIM


class ActionRow(Static):
    class Pressed(Message):
        def __init__(self, action: str) -> None:
            super().__init__()
            self.action = action

    def __init__(
        self,
        subject: str,
        keys: str = "",
        *,
        action: str,
        enabled: bool = True,
        classes: str = "",
        **kwargs,
    ) -> None:
        self.action = action
        self._subject = subject
        self._keys = keys
        self._selected = False
        self.enabled = enabled
        super().__init__(
            self._text(),
            classes=f"onb-option set-group-gap {classes}".strip(),
            **kwargs,
        )

    def _text(self) -> Text:
        text = Text()
        if not self.enabled:
            text.append(f"  {self._subject}", style=DEEP_DIM)
            return text
        text.append("▸ ", style=ACCENT if self._selected else DEEP_DIM)
        text.append(self._subject)
        text.append(f"   {self._keys}", style=DEEP_DIM)
        return text

    def set_content(
        self, subject: str, keys: str = "", *, enabled: bool = True
    ) -> None:
        self._subject = subject
        self._keys = keys
        self.enabled = enabled
        self.update(self._text())

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self.set_class(selected, "selected")
        self.update(self._text())

    def press(self) -> None:
        if self.enabled:
            self.post_message(self.Pressed(self.action))

    def on_click(self, event) -> None:
        self.press()
