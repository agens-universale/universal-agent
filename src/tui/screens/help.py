"""HelpScreen — справка из реестра команд + доп. хоткеи."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from tui.commands import build_default_registry

# Мини-туториал сверху Help (канон: docs_md/tui/mini-tutorial/SPEC.md).
# (команда/клавиша, пояснение) — две колонки рядом.
_QUICKSTART_HELP: tuple[tuple[str, str], ...] = (
    ("F6", "Настройки - URL, токен, модель — /settings (MCP при необходимости)"),
    ("Enter", "Работа - опишите задачу в поле ввода и начните сессию (Enter) — сессия создастся сама"),
    ("F2", "Список диалогов. Выберите диалог через ↑ / ↓ и продолжите сессию (Enter)"),
    ("/new или Ctrl+N", "Начать новый диалог"),
    ("F3", "Диффы правок агента — принять или оменить"),
    ("/view <path>", "Постраничный просмотр файла"),
    ("! git status", "Локальный shell - исполнение консольных команд"),
    ("Esc", "Назад / отмена / выход — зависит от контекста"),
    ("/ или .", "Палитра команд - выберите команду и нажмите Enter или ввыедите команду в поле ввода"),
    ("@", "Упомянуть файл в сообщении"),
    ("мышь или Ctrl+C", "Копировать: выделил — сразу в буфер · Ctrl+V — вставить"),
    ("Ctrl+C", "Стоп агента (если нет выделения)"),
    ("Ctrl+J", "Перенос строки в поле ввода"),
    ("[asking]", "Агент задал вопрос — напишите ответ в поле и Enter"),
)

# Хоткеи, которых нет у отдельной /команды (F1–F6 / Ctrl+… уже в реестре).
# Перенос строки: Ctrl+J надёжен везде; Shift/Ctrl+Enter — только если терминал
# отдаёт отдельную последовательность (Kitty / modifyOtherKeys).
_EXTRA_HOTKEYS: tuple[tuple[str, str], ...] = (
    ("Enter", "Отправить сообщение"),
    ("Esc", "Назад / отмена / выход — зависит от контекста"),
    ("Ctrl+J", "Перенос строки"),
    ("Ctrl+A", "Выделить всё в поле ввода"),
    ("мышь", "В трейсе: выделил — сразу в буфер обмена"),
    ("Ctrl+Shift+C", "То же по клавише; в отличие от Ctrl+C не стопает агента"),
    ("↑ / ↓", "История ввода"),
    ("@", "Упомянуть файл в сообщении"),
    (".", "Палитра команд (как /, без смены раскладки)"),
    ("F6", "Настройки"),
)

# Буфер обмена — не slash-команда; короткая секция в /help.
_CLIPBOARD_HELP: tuple[tuple[str, str], ...] = (
    ("Linux", "Поставьте wl-clipboard или xclip, чтобы вставлять в другие программы"),
    ("без утилит", "Вставьте в поле ввода текста (Ctrl+V), выделите с Shift+мышь, скопируйте хоткеем терминала (Ctrl+Shift+C)"),
)

# Shell (`!`) — не slash-команда; отдельная секция в /help.
_SHELL_HELP: tuple[tuple[str, str], ...] = (
    ("! git status", "Локальный shell (sh/cmd), cwd TUI; Esc/Ctrl+C — отмена"),
    ("! clear [N]", "Убрать bang-вывод из трейса (все или последние N)"),
    ("—", "Без sudo/интерактива (vim, docker -it); вывод только в трейсе"),
)


class HelpScreen(Screen):
    """Справочный экран — колонки «команда | пояснение», блоки с разделителями."""

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            with Horizontal(id="header"):
                yield Static("справка", id="header-left", markup=False)
            # Не фокусируем список: иначе срабатывает #trace:focus { outline },
            # и outline ложится поверх первой строки (выглядит как разорванная
            # рамка-заголовок). Прокрутка мышью остаётся; список короткий.
            trace = VerticalScroll(id="trace")
            trace.can_focus = False
            yield trace
            yield Static("[Esc] назад", id="footer", markup=False)

    def on_mount(self) -> None:
        reg = build_default_registry()
        trace = self.query_one("#trace", VerticalScroll)
        row_i = 0

        row_i = self._mount_section(trace, "как работать", row_i)
        for cmd, desc in _QUICKSTART_HELP:
            self._mount_row(trace, row_i, cmd, desc)
            row_i += 1

        row_i = self._mount_section(trace, "команды", row_i)
        for cmd in reg.list_commands():
            label = f"/{cmd.name}"
            parts: list[str] = []
            if cmd.args:
                parts.append(cmd.args.strip())
            parts.append(cmd.description)
            if cmd.hotkey:
                parts.append(cmd.hotkey)
            self._mount_row(trace, row_i, label, "  ·  ".join(parts))
            row_i += 1

        row_i = self._mount_section(trace, "Shell (!)", row_i)
        for label, desc in _SHELL_HELP:
            self._mount_row(trace, row_i, label, desc)
            row_i += 1

        row_i = self._mount_section(trace, "хоткеи", row_i)
        for key, desc in _EXTRA_HOTKEYS:
            self._mount_row(trace, row_i, key, desc)
            row_i += 1

        row_i = self._mount_section(trace, "буфер обмена", row_i)
        for label, desc in _CLIPBOARD_HELP:
            self._mount_row(trace, row_i, label, desc)
            row_i += 1

    @staticmethod
    def _mount_section(trace: VerticalScroll, title: str, row_i: int) -> int:
        """Заголовок блока + тонкая линия. Возвращает row_i без сдвига зебры."""
        if row_i > 0:
            trace.mount(Static(""))
        trace.mount(Static(f"── {title} ──", classes="help-section", markup=False))
        return row_i

    @staticmethod
    def _mount_row(
        trace: VerticalScroll,
        index: int,
        cmd: str,
        desc: str,
    ) -> None:
        # зебра: нечётные строки — с чуть более светлым фоном (читаемость)
        classes = "help-row help-row-alt" if index % 2 else "help-row"
        row = Horizontal(classes=classes)
        trace.mount(row)
        # слева команда/клавиша, сразу справа пояснение (не через весь экран)
        row.mount(Static(cmd, classes="help-row-cmd", markup=False))
        row.mount(Static(desc, classes="help-row-desc", markup=False))
