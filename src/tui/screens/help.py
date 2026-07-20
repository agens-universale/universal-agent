"""HelpScreen — справка из реестра команд + доп. хоткеи."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from tui.commands import build_default_registry

# Мини-туториал сверху Help (канон: docs_md/tui/mini-tutorial/SPEC.md).
# (hotkey, текст) — hotkey справа; пустой hotkey = только описание.
_QUICKSTART_HELP: tuple[tuple[str, str], ...] = (
    ("F6", "URL, токен, модель — /settings (MCP при необходимости)"),
    ("Enter", "опишите задачу в поле — сессия создастся сама"),
    ("F2", "список диалогов · /new (Ctrl+N) — новый диалог"),
    ("F3", "диффы правок агента — принять или откатить"),
    ("", "/view <path> — постраничный просмотр файла"),
    ("", "! git status — локальный shell; Esc — отмена"),
    ("", "/ или . — палитра команд · @ — приложить файл"),
    ("мышь", "копировать: выделил — сразу в буфер · Ctrl+V — вставить"),
    ("Ctrl+C", "стоп агента (если нет выделения) · Esc — назад / выход"),
    ("Ctrl+J", "перенос строки · [asking] — ответ в поле · Y/S/N — опасные операции"),
)

# Хоткеи, которых нет у отдельной /команды (F1–F6 / Ctrl+… уже в реестре).
# Перенос строки: Ctrl+J надёжен везде; Shift/Ctrl+Enter — только если терминал
# отдаёт отдельную последовательность (Kitty / modifyOtherKeys).
_EXTRA_HOTKEYS: tuple[tuple[str, str], ...] = (
    ("Enter", "отправить сообщение"),
    ("Ctrl+J", "перенос строки"),
    ("Ctrl+A", "выделить всё в поле ввода"),
    ("мышь", "в трейсе: выделил — сразу в буфер обмена"),
    ("Ctrl+Shift+C", "то же по клавише; в отличие от Ctrl+C не стопает агента"),
    ("↑ / ↓", "история ввода"),
    ("@", "упомянуть файл в сообщении"),
    (".", "палитра команд (как /, без смены раскладки)"),
    ("F6", "настройки"),
)

# Буфер обмена — не slash-команда; короткая секция в /help.
_CLIPBOARD_HELP: tuple[tuple[str, str], ...] = (
    ("", "Linux: поставьте wl-clipboard или xclip, чтобы вставлять скопированный текст в другие программы"),
    ("", "без них: сначала Ctrl+V в поле ввода → затем Shift+мышь → Ctrl+Shift+C в окне ввода чтобы скопировать в буфер обмена."),
)

# Shell (`!`) — не slash-команда; отдельная секция в /help.
_SHELL_HELP: tuple[tuple[str, str], ...] = (
    ("! git status", "локальный shell (sh/cmd), cwd TUI; Esc/Ctrl+C — отмена"),
    ("! clear [N]", "убрать bang-вывод из трейса (все или последние N)"),
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

        trace.mount(Static("как работать", classes="help-section", markup=False))
        for key, desc in _QUICKSTART_HELP:
            self._mount_row(trace, row_i, desc, key)
            row_i += 1

        trace.mount(Static(""))
        trace.mount(Static("команды", classes="help-section", markup=False))
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

        trace.mount(Static(""))
        trace.mount(Static("буфер обмена", classes="help-section", markup=False))
        for example, desc in _CLIPBOARD_HELP:
            main = f"{example}   {desc}" if example else desc
            self._mount_row(trace, row_i, main, "")
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
