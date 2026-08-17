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

from gc_tui.toggles import TOGGLES


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
    # Аргумент — путь к файлу: при вводе `/<cmd> ` открывается файловый пикер
    # (fuzzy по проекту, как @-аттач). Папки в нём — только навигация.
    arg_file: bool = False
    # Тоггл палитры (/gate, /answers): Enter циклит значение вместо запуска
    # команды; состояние живёт в tui/toggles.py, не здесь (§ CLAUDE.md Architecture).
    is_toggle: bool = False

    @property
    def requires_args(self) -> bool:
        """True, если без аргумента команду нельзя выполнить (схема `<…>`).

        Опциональные `[…]` (как у /export) — Enter выполняет с дефолтом;
        обязательные `<path>` / `<id>` — Enter подставляет `/cmd ` для ввода.
        """
        a = (self.args or "").strip()
        return bool(a) and not a.startswith("[")


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
        """Порядок регистрации = частота использования (help / палитра)."""
        return list(self._by_name.values())

    def all_names(self) -> list[str]:
        return list(self._by_name) + list(self._alias_to_canonical)


def closest_command(name: str, registry: CommandRegistry) -> Optional[str]:
    if not name:
        return None
    matches = get_close_matches(name, registry.all_names(), n=1, cutoff=0.4)
    return matches[0] if matches else None


def build_default_registry() -> CommandRegistry:
    """Каноничный набор MVP-команд (§4 спеки), по убыванию частоты."""
    reg = CommandRegistry()

    def _cap(s: str) -> str:
        return s[:1].upper() + s[1:] if s else s

    cmds = [
        Command("help", (), "", "Справка по командам", hotkey="F1 · Ctrl+/"),
        Command("sessions", (), "", "Список сессий — выбрать и продолжить",
                hotkey="F2 · Ctrl+S"),
        Command("diff", (), "", "Просмотр pending-диффов, принять/откатить",
                hotkey="F3 · Ctrl+D"),
        Command("view", (), "<path>", "Постраничный просмотр текстового файла",
                arg_file=True),
        Command("notices", (), "", "Уведомления (старт + runtime)",
                hotkey="F4 · Ctrl+O"),
        Command("new", (), "", "Новая сессия в этом окне", hotkey="Ctrl+N"),
        Command("config", (), "", "Конфиг сессии + пути к файлам",
                hotkey="F5 · Ctrl+G"),
        Command("settings", (), "", "Настройки: подключение, модель, MCP, обновление",
                hotkey="F6"),
        Command("export", (), "[txt|json|log-txt|log-json]", "Экспорт сессии",
                arg_options=(("txt", "Вся сессия в читаемом виде"),
                             ("json", "Структурированные данные сессии"),
                             ("log-txt", "Лог-файл агента, текущий запуск (.log)"),
                             ("log-json", "JSONL-файл треда (вся сессия)"))),
        Command("compact", ("сжать",), "", "Сжать контекст сессии сейчас"),
        Command("init", (), "", "Анализ проекта и генерация AGENTS.md"),
        Command("skills", (), "", "Обзор навыков"),
        Command("skill", (), "<id>", "Использовать навык"),
        Command("status", (), "", "Состояние текущей сессии"),
        Command("stop", (), "", "Остановить активную задачу", hotkey="Ctrl+C"),
        Command("mcp", (), "", "MCP-серверы и их тулы"),
        Command("buffer", (), "", "Буфер сессии — текст собранных копий", hotkey="F7"),
        Command("quit", (), "", "Выход", hotkey="Esc"),
        # Переключатели палитры (←/→/Enter): самопроверка хода и формат финала.
        Command(
            "rigor", (), "",
            _cap(f"{TOGGLES['rigor'].label}: {TOGGLES['rigor'].summary}"),
            is_toggle=True,
        ),
        Command(
            "gate", (), "",
            _cap(f"{TOGGLES['gate'].label}: {TOGGLES['gate'].summary}"),
            is_toggle=True,
        ),
        Command(
            "answers", (), "",
            _cap(f"{TOGGLES['answers'].label}: {TOGGLES['answers'].summary}"),
            is_toggle=True,
        ),
    ]
    for c in cmds:
        reg.register(c)
    return reg
