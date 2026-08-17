"""PasteInput — однострочный Input с вставкой из СИСТЕМНОГО буфера (Ctrl+V / правый клик).

Штатный Input.action_paste читает только внутренний app.clipboard (пустой для текста,
скопированного вне приложения) — на Windows/терминалах без bracketed-paste вставка не
работает. Зеркалит логику CommandTextArea (`_paste_from_os_clipboard`), но для Input:
многострочный текст сводится в одну строку (Input однострочный)."""
from __future__ import annotations

from textual import events
from textual.widgets import Input


class PasteInput(Input):
    def _paste_from_os_clipboard(self) -> bool:
        """Вставить текст системного буфера в позицию курсора. True — если что-то вставили."""
        from gc_tui.services.os_ops import read_clipboard
        text = read_clipboard()
        if not text:
            return False
        # Input однострочный: переводы строк сводим (bracketed шлёт \r).
        text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
        if not self.selection.is_empty:      # выделение → заменить, а не дописать (нативное поведение поля)
            self.delete_selection()
        self.insert_text_at_cursor(text)
        return True

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "ctrl+v" and self._paste_from_os_clipboard():
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)

    async def _on_mouse_down(self, event: events.MouseDown) -> None:
        # Правый клик → вставка из системного буфера (при mouse-reporting терминал
        # отдаёт правый клик приложению, нативная «paste по правому клику» не срабатывает).
        if event.button == 3:
            event.stop()
            event.prevent_default()
            self._paste_from_os_clipboard()
            return
        await super()._on_mouse_down(event)
