"""Реестр команд TUI MVP (§4 спеки).

Единый источник для:
- палитры команд (виджет),
- /help (генерится из реестра),
- closest-match (`difflib.get_close_matches`).
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import get_close_matches
from typing import Optional


@dataclass(frozen=True)
class Command:
    name: str
    aliases: tuple[str, ...]
    args: str
    description: str
    hotkey: str = ""
    # Фиксированные варианты значения аргумента (value, описание) — открывают
    # ArgPicker при вводе `/<cmd> `. Пусто = у команды нет такого меню.
    arg_options: tuple[tuple[str, str], ...] = ()


class CommandRegistry:
    def __init__(self) -> None:
        self._by_name: dict[str, Command] = {}
        self._alias_to_canonical: dict[str, str] = {}

    def register(self, cmd: Command) -> None:
        self._by_name[cmd.name] = cmd
        for a in cmd.aliases:
            self._alias_to_canonical[a] = cmd.name

    def lookup(self, name: str) -> Optional[Command]:
        if name in self._by_name:
            return self._by_name[name]
        canonical = self._alias_to_canonical.get(name)
        if canonical:
            return self._by_name.get(canonical)
        return None

    def list_commands(self) -> list[Command]:
        return sorted(self._by_name.values(), key=lambda c: c.name)

    def all_names(self) -> list[str]:
        return list(self._by_name) + list(self._alias_to_canonical)


def closest_command(name: str, registry: CommandRegistry) -> Optional[str]:
    if not name:
        return None
    matches = get_close_matches(name, registry.all_names(), n=1, cutoff=0.4)
    return matches[0] if matches else None


def build_default_registry() -> CommandRegistry:
    """Каноничный набор MVP-команд (§4 спеки)."""
    reg = CommandRegistry()
    cmds = [
        Command("sessions", (), "", "список сессий — выбрать и продолжить", hotkey="Ctrl+S"),
        Command("new", (), "", "новая сессия в этом окне", hotkey="Ctrl+N"),
        Command("stop", (), "", "остановить активную задачу", hotkey="Ctrl+C"),
        Command("skill", (), "<id>", "использовать навык"),
        Command("skills", (), "", "обзор навыков"),
        Command("status", (), "", "состояние текущей сессии"),
        Command("config", (), "", "конфиг сессии + пути к файлам", hotkey="F2"),
        Command("mcp", (), "", "MCP-серверы и их тулы", hotkey="F3"),
        Command("notices", (), "", "уведомления (старт + runtime)", hotkey="F4"),
        Command("export", (), "[txt|json|log-txt|log-json]", "экспорт сессии",
                arg_options=(("txt", "вся сессия в читаемом виде"),
                             ("json", "структурированные данные сессии"),
                             ("log-txt", "лог-файл агента, текущий запуск (.log)"),
                             ("log-json", "jsonl-файл треда (вся сессия)"))),
        Command("help", (), "", "справка по командам", hotkey="F1"),
        Command("init", (), "", "анализ проекта и генерация AGENTS.md"),
        Command("compact", ("сжать",), "", "сжать контекст сессии сейчас"),
        Command("quit", (), "", "выход", hotkey="Esc"),
    ]
    for c in cmds:
        reg.register(c)
    return reg
