"""Клик-модель строк трейса — единая точка контракта взаимодействия.

Мышь и клавиатура симметричны: {клик ↔ Tab} выделение, {Ctrl+клик ↔ Enter}
выделение + открытие экрана события. Двойной клик события не открывает
(заменён Ctrl+кликом). Меняешь модификатор или порядок действий — меняешь
здесь, а не в четырёх виджетах (TraceRow, ToolCallRow, UserReplyContainer,
BlockPanel).
"""
from __future__ import annotations

from typing import Optional

from textual import events
from textual.message import Message


class ClickModelMixin:
    """Клик = выделить строку (как Tab); Ctrl+клик = выделить и открыть (как Enter).

    Наследник реализует ``_open_request()`` — Message открытия своего экрана,
    или None, если строке нечего открывать (тогда Ctrl+клик ведёт себя как
    обычный клик). Наследник со своим контрактом (SummaryMarker) переопределяет
    ``on_click``/``on_key`` целиком.
    """

    # FOCUS_ON_CLICK намеренно НЕ переопределён (наследуется True): Textual
    # фокусирует строку уже на MouseDown (screen.py, set_focus при нажатии) —
    # выделение видно в момент нажатия, в т.ч. при Ctrl+клике ДО того, как
    # экран события накроет трейс. self.focus() в on_click остаётся как
    # идемпотентная страховка (синтетические клики в тестах идут мимо
    # mousedown-пути экрана), а event.stop() — защита от перехвата фокуса
    # контейнером #trace (main.py.on_click) — живёт на Click-уровне.

    def _open_request(self) -> Optional[Message]:
        raise NotImplementedError

    def on_click(self, event: events.Click) -> None:
        if event.ctrl:
            message = self._open_request()
            if message is not None:
                event.stop()
                if self.can_focus:
                    # Ctrl+клик тоже выделяет: после выхода из открытого экрана
                    # фокус остаётся на этой строке, а не на прошлом выделении.
                    self.focus()
                self.post_message(message)
                return
        if self.can_focus:
            event.stop()  # не отдать фокус контейнеру #trace (main.py.on_click)
            self.focus()

    def on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            message = self._open_request()
            if message is not None:
                event.stop()
                self.post_message(message)
