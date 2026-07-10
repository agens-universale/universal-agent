"""HelpScreen — справка из реестра команд + доп. хоткеи."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from tui.commands import build_default_registry

# Хоткеи, которых нет у отдельной /команды (F1–F5 / Ctrl+N / … уже в реестре).
# Перенос строки: Ctrl+J надёжен везде; Shift/Ctrl+Enter — только если терминал
# отдаёт отдельную последовательность (Kitty / modifyOtherKeys).
_EXTRA_HOTKEYS: tuple[tuple[str, str], ...] = (
    ("Enter", "отправить сообщение"),
    ("Ctrl+J", "перенос строки"),
    ("Ctrl+A", "принять все pending-диффы"),
    ("↑ / ↓", "история ввода"),
    ("@", "упомянуть файл в сообщении"),
)

# Shell (`!`) — не slash-команда; отдельная секция в /help.
_SHELL_HELP: tuple[tuple[str, str], ...] = (
    ("! git status", "локальный shell (sh/cmd), cwd TUI; Esc/Ctrl+C — отмена"),
    ("", "без sudo/интерактива (vim, docker -it); вывод только в трейсе"),
)


class HelpScreen(Screen):
    """Справочный экран — команды из реестра + доп. хоткеи (с хоткеями справа)."""

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
        for cmd in reg.list_commands():
            args_str = f" {cmd.args}" if cmd.args else ""
            self._mount_row(
                trace,
                row_i,
                f"/{cmd.name}{args_str}   {cmd.description}",
                cmd.hotkey,
            )
            row_i += 1

        trace.mount(Static(""))
        trace.mount(Static("Shell (!)", classes="help-section", markup=False))
        for example, desc in _SHELL_HELP:
            main = f"{example}   {desc}" if example else desc
            self._mount_row(trace, row_i, main, "")
            row_i += 1

        trace.mount(Static(""))
        trace.mount(Static("хоткеи", classes="help-section", markup=False))
        for key, desc in _EXTRA_HOTKEYS:
            self._mount_row(trace, row_i, desc, key)
            row_i += 1

    @staticmethod
    def _mount_row(
        trace: VerticalScroll,
        index: int,
        main: str,
        hotkey: str = "",
    ) -> None:
        # зебра: нечётные строки — с чуть более светлым фоном (читаемость)
        classes = "help-row help-row-alt" if index % 2 else "help-row"
        row = Horizontal(classes=classes)
        trace.mount(row)
        row.mount(Static(main, classes="help-row-main", markup=False))
        if hotkey:
            row.mount(Static(hotkey, classes="help-row-key", markup=False))
