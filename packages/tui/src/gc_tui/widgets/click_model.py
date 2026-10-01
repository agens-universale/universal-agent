"""Клик-модель строк трейса — единая точка контракта взаимодействия.

Мышь и клавиатура симметричны: {клик ↔ Tab} выделение, {Ctrl+клик ↔ Enter}
выделение + открытие экрана события. Двойной клик события не открывает
(заменён Ctrl+кликом). Меняешь модификатор или порядок действий — меняешь
здесь, а не в четырёх виджетах (TraceRow, ToolCallRow, UserReplyContainer,
BlockPanel).

Симметрия распространяется и на гейт security-confirm: экран события не
открывает НИ Enter, НИ Ctrl+клик, пока баннер ждёт ответа (`confirm_is_active`).

Исключение — картинка внутри строки (плашка реплики, её строка под репликой): она
ведёт себя как ссылка, как кнопка «скопировать» у блока кода, — обычный клик по ней
открывает файл (`_plain_click_image_path`), под тем же гейтом.
"""

from __future__ import annotations

from typing import Optional

from textual import events
from textual.message import Message

from gc_tui.messages import OpenImageRequest


def confirm_is_active(widget) -> bool:
    """Висит ли на экране security-confirm, ждущий ответа юзера.

    Общий предикат для обеих половин клик-модели: пока баннер не отвечен,
    строка не открывает свой экран — иначе он накрывает баннер, тот перестаёт
    получать клавиши, а агент продолжает ждать ответа, которого уже не видно.
    """
    try:
        return bool(getattr(widget.screen, "_confirm_active", False))
    except Exception:
        return False


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
    # контейнером #trace (main.py.on_click) — живёт на Click-уровне.  # noqa: ERA001  # описание формата, не выключенный код

    def _open_request(self) -> Optional[Message]:
        raise NotImplementedError

    def _image_path(self, image_id: str) -> Optional[str]:
        """Файл картинки по `image_id` из meta спана под курсором (плашка или
        подстрока вложения). У строк без картинок — None."""
        return None

    def _plain_click_image_path(self, event: events.Click) -> Optional[str]:
        """Файл картинки, если обычный клик — клик по ней как по ссылке (строки с
        картинками решают, засчитан ли жест). У строк без картинок — None."""
        return None

    def on_click(self, event: events.Click) -> None:
        # Мышиная половина того же гейта, что и Enter в on_key: пока висит
        # security-confirm, Ctrl+клик не открывает detail/viewer — иначе экран
        # события накрывает баннер, [Y] уходит в него, а агент ждёт ответа,
        # которого юзер уже не видит. Ctrl+клик при этом ведёт себя как обычный
        # клик (просто выделяет строку) — обычный клик не трогаем.
        if event.ctrl and not confirm_is_active(self):
            # Картинка — по месту клика (дизайн картинок §20.2): Screen кладёт стиль
            # ячейки под курсором в event.style, спан плашки/подстроки несёт image_id.
            path = self._image_path(event.style.meta.get("image_id") or "")
            message = OpenImageRequest(path) if path else self._open_request()
            if message is not None:
                event.stop()
                if self.can_focus:
                    # Ctrl+клик тоже выделяет: после выхода из открытого экрана
                    # фокус остаётся на этой строке, а не на прошлом выделении.
                    self.focus()
                self.post_message(message)
                return
        if not event.ctrl and not confirm_is_active(self):
            path = self._plain_click_image_path(event)
            if path:
                event.stop()
                self.post_message(OpenImageRequest(path))
                return
        if self.can_focus:
            event.stop()  # не отдать фокус контейнеру #trace (main.py.on_click)
            self.focus()

    def on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            # Confirm-баннер перехватывает Enter на Screen, но focused-строка
            # получает Key раньше (bubble). Не открывать detail/viewer, пока
            # висит security-confirm — иначе [Y]/Enter открывает tool detail.
            if confirm_is_active(self):
                return
            message = self._open_request()
            if message is not None:
                event.stop()
                self.post_message(message)
