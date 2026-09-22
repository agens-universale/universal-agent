"""CopyBufferScreen — экран буфера сессии (SPEC 2026-07-25 §3.1 B6/B7/B8).

Большое текстовое поле на весь экран, длинные строки переносятся, скролл мышью
и клавишами. Открывается сверху. Без анимаций и тиков. Esc закрывает и сохраняет
то, что в поле (пустой текст = очистили буфер); без изменений — на диск не пишем.
Paste: OS-first, откат к внутреннему app.clipboard (как в обычном поле ввода).
Копирование/Ctrl+C не дописывает буфер сам в себя — маркер `_is_copy_buffer_screen`
гейтит `_push_session_copy_buffer` в app.py (B8).
"""

from __future__ import annotations

from typing import Optional

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Static, TextArea

from gc_tui.services.copy_buffer import COPY_BUFFER_MAX, clamp_copy_buffer


class BufferTextArea(TextArea):
    """TextArea с OS-first paste (B8). Без сабмита по Enter — это scratchpad."""

    # Штатный TextArea биндит только ctrl+v/super+v. Ctrl+Shift+V на Linux
    # часто ест терминал (bracketed paste), но если ключ дошёл до приложения —
    # без binding вставка молчит. Оба жеста → тот же action_paste (OS-first).
    BINDINGS = [
        Binding("ctrl+v", "paste", "вставить", show=False),
        Binding("ctrl+shift+v", "paste", "вставить", show=False),
        # B11: Ctrl+A — выделить всё. Штатный TextArea биндит ctrl+a → cursor_line_start,
        # а select_all на f7 (который перехватывается app-уровнем action_buffer).
        Binding("ctrl+a", "select_all", "выделить всё", show=False),
    ]

    def _paste_from_os_clipboard(self) -> bool:
        """Вставить текст системного буфера. True — если что-то вставили."""
        from gc_tui.services.os_ops import read_clipboard

        text = read_clipboard()
        if text:
            self.insert(text)
            return True
        return False

    def action_paste(self) -> None:
        """Ctrl+V / Ctrl+Shift+V: сначала системный буфер, пусто — внутренний.

        Базовый TextArea.action_paste читает внутренний app.clipboard (только то,
        что скопировали внутри приложения). Берём системный буфер; пусто —
        откатываемся к штатному поведению (внутренний буфер TUI).
        """
        if not self._paste_from_os_clipboard():
            super().action_paste()

    async def _on_mouse_down(self, event: events.MouseDown) -> None:
        """Правый клик → вставка из системного буфера (как в поле ввода)."""
        if event.button == 3:
            event.stop()
            event.prevent_default()
            self._paste_from_os_clipboard()
            return
        await super()._on_mouse_down(event)


class CopyBufferScreen(Screen):
    """Экран буфера сессии. Esc = сохранить и закрыть (B7)."""

    _is_copy_buffer_screen = True

    BINDINGS = [
        ("escape", "save_and_close", "сохранить и закрыть"),
    ]

    def __init__(
        self,
        *,
        store=None,
        task_id: Optional[str] = None,
        footer_hint: str = "",
    ) -> None:
        super().__init__()
        self._store = store
        self._active_task_id = task_id
        self._footer_hint = footer_hint or (
            "Esc — сохранить и закрыть · Ctrl+V / Ctrl+Shift+V вставить · длинные строки переносятся"
        )
        # Исходный текст — для сравнения «что-то меняли» (B7).
        self._initial_text: str = ""

    def _load_existing(self) -> str:
        if self._store is None or not self._active_task_id:
            return ""
        try:
            meta = self._store.get_task_meta(self._active_task_id) or {}
            return (meta.get("tui") or {}).get("copy_buffer") or ""
        except Exception:
            return ""

    def compose(self) -> ComposeResult:
        self._initial_text = self._load_existing()
        with Vertical(id="root"):
            with Horizontal(id="header"):
                yield Static("Буфер сессии", id="header-left", markup=False)
            yield BufferTextArea(
                self._initial_text,
                id="buffer_text",
                show_line_numbers=False,
                soft_wrap=True,
            )
            yield Static(self._footer_hint, id="footer", markup=False)

    def on_mount(self) -> None:
        self.query_one("#buffer_text", BufferTextArea).focus()

    @staticmethod
    def _normalize_for_save(text: str) -> str:
        """B11: strip краёв и сжать чистый whitespace до пустой.

        Если остались только переносы строк и пробелы — пустая строка
        (= очистили буфер). Иначе — strip краевых пустых строк (общий strip)
        и хвостовых пробелов каждой строки; лидирующие отступы сохраняем.
        """
        # rstrip каждой строки убирает хвостовые пробелы/табы; общий strip
        # убирает краевые пустые строки. Чистый whitespace → "".
        lines = [ln.rstrip() for ln in text.splitlines()]
        return "\n".join(lines).strip()

    def action_save_and_close(self) -> None:
        """B7+B11: сохранить если меняли (пусто = очистили); без изменений — no-write.

        Чистый whitespace (переносы/пробелы) сжимается до пустой строки.
        """
        ta = self.query_one("#buffer_text", BufferTextArea)
        current = self._normalize_for_save(ta.text)
        normalized_initial = self._normalize_for_save(self._initial_text)
        if current == normalized_initial:
            self.app.pop_screen()
            return
        if self._store is not None and self._active_task_id:
            try:
                saved = clamp_copy_buffer(current, max_size=COPY_BUFFER_MAX)
                self._store.update_tui_meta(self._active_task_id, {"copy_buffer": saved})
            except Exception:
                pass  # best-effort: не роняем закрытие экрана
        self.app.pop_screen()
