"""PasteInput — однострочный Input с вставкой из СИСТЕМНОГО буфера (Ctrl+V / правый клик).

Штатный Input.action_paste читает только внутренний app.clipboard (пустой для текста,
скопированного вне приложения) — на Windows/терминалах без bracketed-paste вставка не
работает. Зеркалит логику CommandTextArea (`_paste_from_os_clipboard`), но для Input:
многострочный текст сводится в одну строку (Input однострочный)."""

from __future__ import annotations

from textual import events
from textual.widgets import Input


class PasteInput(Input):
    def _sync_group_disabled(self) -> None:
        """Отключённое поле в группе гасит рамку ГРУППЫ (единый язык форм §3.3).

        Своей рамки у поля внутри `.field-group` нет — класс `set-framed` оно
        теряет, иначе рамок было бы две, — поэтому правило одинокого поля
        (`#conn-token.set-framed:disabled`) сюда не достаёт. Селектора `:has`
        в Textual 8.2.7 нет, так что признак ставим отсюда: это единственное
        место, через которое проходит любое изменение `disabled` у поля формы.
        Поле вне группы — no-op: у него своя рамка и своё правило."""
        parent = self.parent
        if parent is not None and parent.has_class("field-group"):
            parent.set_class(self.disabled, "set-disabled")

    def watch_disabled(self, disabled: bool) -> None:
        super().watch_disabled(disabled)
        self._sync_group_disabled()

    def on_mount(self) -> None:
        # Поле, отключённое ЕЩЁ ДО монтирования (`PasteInput(disabled=True)` —
        # так строятся поля-факты голосового редактора), watcher уже отработало
        # вхолостую: родителя тогда не было. Догоняем на монтировании.
        self._sync_group_disabled()

    def _paste_from_os_clipboard(self) -> bool:
        """Вставить текст системного буфера в позицию курсора. True — если что-то вставили."""
        from gc_tui.services.os_ops import read_clipboard

        text = read_clipboard()
        if not text:
            return False
        # Input однострочный: переводы строк сводим (bracketed шлёт \r).
        text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
        if not self.selection.is_empty:  # выделение → заменить, а не дописать (нативное поведение поля)
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
