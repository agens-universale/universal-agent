"""HelpScreen — справка из реестра команд."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from tui.commands import build_default_registry
from tui import branding

# Rich-разметка: [bold], [dim], [italic], [#hex]цвет[/].
# Ссылки: [link="https://..."]текст[/link] — URL в кавычках (иначе : ломает парсер).
_HELP_FOOTER = """
[bold]Связь с разработчиками и поддержка[/bold]
Баги, пожелания, обратная связь
[link="https://t.me/+2AwDQhHxaAdhNmUy"]В Телеграм[/link]
[link="https://sberchat.sberbank.ru/@gc-ua-support"]В Сберчат[/link]
""".strip()


class HelpScreen(Screen):
    """Справочный экран — все команды из реестра (с хоткеями справа)."""

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            with Horizontal(id="header"):
                yield Static("Справка", id="header-left", markup=False)
                yield Static(branding.HEADER_RIGHT_PLAIN, id="header-right", markup=False)
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
        for i, cmd in enumerate(reg.list_commands()):
            args_str = f" {cmd.args}" if cmd.args else ""
            # зебра: нечётные строки — с чуть более светлым фоном (читаемость)
            classes = "help-row help-row-alt" if i % 2 else "help-row"
            row = Horizontal(classes=classes)
            trace.mount(row)
            row.mount(Static(
                f"/{cmd.name}{args_str}   {cmd.description}",
                classes="help-row-main", markup=False,
            ))
            if cmd.hotkey:
                row.mount(Static(cmd.hotkey, classes="help-row-key", markup=False))
        trace.mount(Static(_HELP_FOOTER, classes="help-footer", markup=True))
