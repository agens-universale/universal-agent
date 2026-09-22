"""CommandTextArea — многострочный ввод: Enter отправляет, Ctrl+J / Shift+Enter / Ctrl+Enter — новая строка."""

from __future__ import annotations

from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.widgets import TextArea

from gc_tui.services.paste_chips import ChipStore, expand, should_collapse, token_spans

# Константы вместо повторяющихся литералов (Sonar S1192).
_ACT_SELECT_ALL = "выделить всё"


class CommandTextArea(TextArea):
    """Multi-line input. Enter submits; Ctrl+J / Shift+Enter / Ctrl+Enter insert newline.

    Использует нативное сообщение ``TextArea.Changed`` для live-фильтра палитры;
    собственное ``Submitted`` постится по Enter (без newline-модификатора).
    """

    # Textual TextArea: ctrl+a = cursor_line_start. Нам нужен select-all
    # (Backspace/копирование); priority перебивает дефолт + ЙЦУКЕН.
    BINDINGS = [
        Binding("ctrl+a", "select_all", _ACT_SELECT_ALL, show=False, priority=True),
        Binding("ctrl+ф", "select_all", _ACT_SELECT_ALL, show=False, priority=True),
        Binding("ctrl+A", "select_all", _ACT_SELECT_ALL, show=False, priority=True),
        Binding("ctrl+Ф", "select_all", _ACT_SELECT_ALL, show=False, priority=True),
        # macOS: option+backspace (терминал в режиме Option-as-Meta шлёт alt+backspace).
        # В дефолтах TextArea клавиша не занята; Win/Linux — прежнее поведение контрактом.
        Binding("alt+backspace", "delete_word_left", "удалить слово", show=False),
    ]

    class Submitted(Message):
        """Posted when user presses Enter (without newline-modifier)."""

        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    class PasteCollapsed(Message):
        """Posted when a large paste was collapsed into a chip (screen shows a hint)."""

    # keys that insert a newline instead of submitting.
    # ctrl+j — надёжно во всех терминалах (LF).
    # shift/ctrl+enter — Kitty keyboard protocol.
    # shift/ctrl+\r — xterm modifyOtherKeys / fixterms (Windows Terminal и др.).
    _NEWLINE_KEYS = {
        "ctrl+j",
        "shift+enter",
        "ctrl+enter",
        "shift+\r",
        "ctrl+\r",
    }

    def __init__(self, *args, **kwargs) -> None:
        # Плоский ввод без gutter с номерами строк, как обычный Input.
        kwargs.setdefault("show_line_numbers", False)
        super().__init__(*args, **kwargs)
        # Состояние «плашек» объёмной вставки (когезивно с буфером ввода).
        self._chips = ChipStore()
        self._expand_chip_id: int | None = None  # последняя свёрнутая плашка (без таймера)

    async def _on_key(self, event: events.Key) -> None:
        if event.key in self._NEWLINE_KEYS:
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self.submission_text()))
            return
        # let TextArea handle other keys normally (printable insert, navigation, …)
        await super()._on_key(event)

    # --- paste chips --------------------------------------------------

    def submission_text(self) -> str:
        """Текст для отправки: буфер с развёрнутыми плашками по их местам."""
        return expand(self.text, self._chips)

    def _clear_chip_state(self) -> None:
        self._chips.reset()
        self._close_expand_window()

    def _on_text_area_changed(self, event: TextArea.Changed) -> None:
        # Буфер опустел (отправка/очистка) — плашки потеряли привязку к тексту.
        # Сбрасываем стор здесь, а не в Enter-ветке: Enter теперь не всегда
        # «отправка» (стоп записи голосом оставляет текст в поле).
        if not self.text:
            self._clear_chip_state()

    def _insert_replacing_selection(self, text: str) -> None:
        start, end = sorted(self.selection)
        result = self.replace(text, start, end)
        self.move_cursor(result.end_location)

    def _open_expand_window(self, chip_id: int) -> None:
        # Запомнить последнюю свёрнутую плашку — повторная вставка её текста
        # развернёт инлайн в любой момент (без таймера). Сбрасывается при самом
        # развороте, вставке чего-то другого или отправке.
        self._expand_chip_id = chip_id

    def _close_expand_window(self) -> None:
        self._expand_chip_id = None

    def _expand_chip_inline(self, chip_id: int) -> None:
        full = self._chips.get(chip_id)
        if full is None:
            return
        for cid, row, c0, c1 in token_spans(self.text, self._chips):
            if cid == chip_id:
                result = self.replace(full, (row, c0), (row, c1))
                self.move_cursor(result.end_location)
                break
        self._chips.remove(chip_id)

    def _handle_paste(self, text: str) -> None:
        """Единая точка сведения трёх путей вставки."""
        # Нормализуем концы строк: bracketed-paste (Ctrl+V в Windows Terminal и пр.)
        # шлёт \r как разделитель, а should_collapse/chip_label считают \n —
        # иначе многострочная \r-вставка не сворачивается (уходит инлайн).
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        # 1. та же паста в окне «развернуть» → развернуть последнюю плашку инлайн
        #    (только если плашка ещё в буфере; иначе — обычная вставка ниже:
        #    Esc/очистка могли стереть label, оставив orphan expand-окно)
        if self._expand_chip_id is not None and text == self._chips.get(self._expand_chip_id):
            chip_id = self._expand_chip_id
            if any(cid == chip_id for cid, *_ in token_spans(self.text, self._chips)):
                self._expand_chip_inline(chip_id)
                self._close_expand_window()
                return
            self._chips.remove(chip_id)
            self._close_expand_window()
        # 2. большая вставка → плашка + окно + подсказка
        if should_collapse(text):
            chip_id, label = self._chips.add(text)
            self._insert_replacing_selection(label)
            self._open_expand_window(chip_id)
            self.post_message(self.PasteCollapsed())
            return
        # 3. обычная вставка
        self._insert_replacing_selection(text)
        self._close_expand_window()

    async def _on_paste(self, event: events.Paste) -> None:
        """Нативная (bracketed) вставка — через нашу логику плашек.

        prevent_default обязателен: без него Textual проходит MRO и вызывает ещё и
        базовый TextArea._on_paste, который вставит сырой текст (дубль). См. _on_key."""
        if self.read_only:
            return
        event.stop()
        event.prevent_default()
        self._handle_paste(event.text)

    # --- атомарность плашки -------------------------------------------

    def _chip_span_at(self, *, right_border: bool, left_border: bool):
        """Вернуть (cid, row, c0, c1), если курсор у соответствующей границы плашки."""
        row, col = self.cursor_location
        for cid, r, c0, c1 in token_spans(self.text, self._chips):
            if r != row:
                continue
            if right_border and col == c1:
                return (cid, r, c0, c1)
            if left_border and col == c0:
                return (cid, r, c0, c1)
        return None

    def _remove_chip_span(self, span) -> None:
        cid, r, c0, c1 = span
        self.delete((r, c0), (r, c1))
        self._chips.remove(cid)
        if cid == self._expand_chip_id:
            self._close_expand_window()

    def action_delete_left(self) -> None:
        if self.selection.is_empty:
            span = self._chip_span_at(right_border=True, left_border=False)
            if span is not None:
                self._remove_chip_span(span)
                return
        super().action_delete_left()

    def action_delete_right(self) -> None:
        if self.selection.is_empty:
            span = self._chip_span_at(right_border=False, left_border=True)
            if span is not None:
                self._remove_chip_span(span)
                return
        super().action_delete_right()

    def action_cursor_left(self, select: bool = False) -> None:
        if not select and self.selection.is_empty:
            span = self._chip_span_at(right_border=True, left_border=False)
            if span is not None:
                self.move_cursor((span[1], span[2]))  # прыжок к левой границе
                return
        super().action_cursor_left(select)

    def action_cursor_right(self, select: bool = False) -> None:
        if not select and self.selection.is_empty:
            span = self._chip_span_at(right_border=False, left_border=True)
            if span is not None:
                self.move_cursor((span[1], span[3]))  # прыжок к правой границе
                return
        super().action_cursor_right(select)

    def _normalize_cursor(self) -> None:
        """Если курсор оказался внутри плашки (клик мышью) — снап на ближайшую границу."""
        row, col = self.cursor_location
        for _cid, r, c0, c1 in token_spans(self.text, self._chips):
            if r == row and c0 < col < c1:
                self.move_cursor((r, c0 if (col - c0) <= (c1 - col) else c1))
                return

    # --- визуальная геометрия курсора (для ↑/↓ history-навигации экрана) ----

    def cursor_on_first_visual_line(self) -> bool:
        """Курсор на самой верхней ВИЗУАЛЬНОЙ строке (с учётом soft-wrap).

        Логический ``cursor_location[0] == 0`` недостаточен: длинная строка
        переносится на несколько визуальных — курсор может быть в row 0, но
        визуально ниже. Экран спрашивает это, решая «↑ → история или вверх».
        """
        return self.wrapped_document.location_to_offset(self.cursor_location).y == 0

    def cursor_on_last_visual_line(self) -> bool:
        """Курсор на самой нижней ВИЗУАЛЬНОЙ строке (с учётом soft-wrap)."""
        wd = self.wrapped_document
        return wd.location_to_offset(self.cursor_location).y == wd.height - 1

    def _paste_from_os_clipboard(self) -> bool:
        """Вставить текст системного буфера. True — если что-то вставили."""
        from gc_tui.services.os_ops import read_clipboard

        text = read_clipboard()
        if text:
            self._handle_paste(text)
            return True
        return False

    def action_paste(self) -> None:
        """Ctrl+V: вставить из системного буфера.

        Базовый TextArea.action_paste читает только внутренний app.clipboard
        (пустой для текста, скопированного вне приложения). Берём системный
        буфер; пусто — откатываемся к штатному поведению.
        bracketed-paste (events.Paste) не затрагивается — там текст приходит сам.
        """
        if not self._paste_from_os_clipboard():
            super().action_paste()

    async def _on_mouse_down(self, event: events.MouseDown) -> None:
        """Правый клик → вставка из системного буфера.

        При включённом mouse-reporting терминал отдаёт правый клик приложению как
        mouse-событие, и нативная «paste по правому клику» терминала не срабатывает.
        Обрабатываем сами — UX как в обычном терминале. Остальные кнопки —
        штатное поведение TextArea (позиционирование курсора / выделение).
        """
        if event.button == 3:
            event.stop()
            event.prevent_default()
            self._paste_from_os_clipboard()
            return
        await super()._on_mouse_down(event)
        self._normalize_cursor()
