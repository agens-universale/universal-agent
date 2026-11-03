"""Реестр команд TUI MVP (§4 спеки).

Единый источник для:
- палитры команд (виджет),
- /help (генерится из реестра),
- closest-match (`difflib.get_close_matches`).
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import get_close_matches
from typing import Any, Callable, Optional

from rich.text import Text

from gc_tui.services.model_picker import (
    model_hint,
    model_name,
    model_picker_options,
    model_value,
)
from gc_tui.services.profile_lock import profile_switch_refusal
from gc_tui.toggles import (
    TOGGLES,
    profile_name,
    profile_picker_hint,
    profile_picker_options,
    profile_value,
)


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
    # Тоггл палитры (/gate, /answers): ←/→ меняют значение, Enter закрывает
    # палитру вместо запуска команды; состояние живёт в tui/toggles.py,
    # не здесь (§ CLAUDE.md Architecture).
    is_toggle: bool = False
    # Динамические варианты аргумента (app -> [(value, описание)]): ArgPicker для
    # команд, чей список известен только в рантайме (/profile — файлы профилей).
    # Взаимоисключимо с arg_options; хинт подвала пикера — arg_hint_provider.
    arg_options_provider: Optional[Callable[[Any], tuple]] = None
    arg_hint_provider: Optional[Callable[[Any, Optional[str]], str]] = None
    # Текущее значение аргумента (app -> value): на нём ArgPicker
    # открывается (`/model` — действующий выбор).
    arg_current_provider: Optional[Callable[[Any], Optional[str]]] = None
    # Enter по строке пикера только выбирает значение (`/model`,
    # `/profile`), а не запускает действие: строка клавиш — «Enter выбрать».
    arg_picks: bool = False
    # Причина, по которой выбор значения сейчас закрыт (app -> str | None):
    # пикер листается, но Enter строку не берёт (`/profile` во время хода).
    arg_lock_provider: Optional[Callable[[Any], Optional[str]]] = None
    # Текущее значение команды (app -> Text) — палитра показывает его в
    # правой колонке вместо хоткея (`/model` — знак модели, `/profile` —
    # имя профиля).
    value_provider: Optional[Callable[[Any], Optional[Text]]] = None

    @property
    def runs_on_enter(self) -> bool:
        """Enter по строке палитры выполняет команду сразу: аргумента нет либо
        он необязателен (``[…]``) и меню вариантов у него нет — значение
        набирается руками (``/plan clear``). Иначе Enter подставляет команду
        в поле: следом откроется меню или ввод аргумента."""
        if not self.args:
            return True
        has_menu = (
            bool(self.arg_options)
            or self.arg_options_provider is not None
            or self.arg_file
        )
        return self.args.startswith("[") and not has_menu


class CommandRegistry:
    def __init__(self) -> None:
        self._by_name: dict[str, Command] = {}
        self._alias_to_canonical: dict[str, str] = {}

    def register(self, cmd: Command) -> None:
        """Имя и алиасы команды не должны быть заняты ничьим именем или
        алиасом: иначе одно `/слово` вело бы в две команды."""
        for word in (cmd.name, *cmd.aliases):
            if word in self._by_name or word in self._alias_to_canonical:
                raise ValueError(f"/{word} уже занято другой командой")
        self._by_name[cmd.name] = cmd
        for a in cmd.aliases:
            self._alias_to_canonical[a] = cmd.name

    def lookup(self, name: str) -> Optional[Command]:
        """Команда по имени или алиасу ровно как набрано."""
        canonical = self._alias_to_canonical.get(name, name)
        return self._by_name.get(canonical)

    def list_commands(self) -> list[Command]:
        """Порядок регистрации = частота использования (help / палитра)."""
        return list(self._by_name.values())

    def all_names(self) -> list[str]:
        return list(self._by_name) + list(self._alias_to_canonical)


def field_word(cmd: Command, word: str) -> str:
    """Как набранное слово команды пишется в поле — латиницей: русский
    алиас становится именем команды (`очистить` → `new`), английский
    синоним и имя остаются как есть."""
    return word if word.isascii() else cmd.name


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
        Command(
            "help",
            ("?", "справка", "помощь"),
            "",
            "Справка по командам",
            hotkey="F1 · Ctrl+/",
        ),
        Command(
            "sessions",
            ("resume", "continue", "сессии", "сессия"),
            "",
            "Список сессий — выбрать и продолжить",
            hotkey="F2 · Ctrl+S",
        ),
        # Чем работаем (спека model-knob §6.2): обычные команды со списком
        # значений; текущее значение — справа, в колонке клавиш.
        Command(
            "model",
            ("models", "модель", "модели"),
            "[модель]",
            _cap(f"{TOGGLES['model'].label}: {TOGGLES['model'].summary}"),
            arg_options_provider=model_picker_options,
            arg_hint_provider=model_hint,
            arg_current_provider=model_name,
            arg_picks=True,
            value_provider=model_value,
        ),
        Command(
            "profile",
            ("profiles", "профиль", "профили"),
            "[профиль]",
            _cap(
                f"{TOGGLES['profiles'].label}: {TOGGLES['profiles'].summary}"
            ),
            arg_options_provider=profile_picker_options,
            arg_hint_provider=profile_picker_hint,
            arg_current_provider=profile_name,
            arg_picks=True,
            arg_lock_provider=profile_switch_refusal,
            value_provider=profile_value,
        ),
        Command(
            "diff",
            ("правки", "дифф"),
            "",
            "Правки агента: обзор, принятие, откат",
            hotkey="F3 · Ctrl+D",
        ),
        Command(
            "view",
            ("просмотр",),
            "<path>",
            "Постраничный просмотр текстового файла",
            arg_file=True,
        ),
        Command(
            "notices",
            ("уведомления",),
            "",
            "Уведомления (старт + runtime)",
            hotkey="F4 · Ctrl+O",
        ),
        Command(
            "new",
            ("clear", "новая", "очистить"),
            "",
            "Новая сессия в этом окне",
            hotkey="Ctrl+N",
        ),
        Command(
            "plan",
            ("план",),
            "[clear]",
            "Вернуться к плану сессии; clear — убрать план",
            # Без arg_options намеренно: меню аргументов открывается с
            # выделенным первым вариантом, и Enter после `/plan` выполнял
            # `clear` — разрушительное действие вместо ожидаемого показа
            # плана (живая приёмка). `clear` набирается руками.
        ),
        Command(
            "config",
            ("конфиг",),
            "",
            "Конфиг сессии + пути к файлам",
            hotkey="F5 · Ctrl+G",
        ),
        # Перечисляет секции экрана (`SECTIONS`, tui/screens/settings.py) — строку
        # видно при каждом открытии палитры, расхождение с составом читается как
        # «секции нет». Без ведущего «Настройки:»: имя команды стоит колонкой слева,
        # а описание тут самое длинное — префикс двигал бы колонку хоткеев вправо
        # ради слова, которое уже прочитано.
        Command(
            "settings",
            ("настройки",),
            "",
            "Подключение, модели, профили, MCP, голос, режимы, внешний вид",
            hotkey="F6",
        ),
        # Стоп и отмена здесь не перечисляются: их показывает сама пилюля записи
        # («Ctrl+T / Enter — стоп · Esc — отмена»), пока диктовка идёт.
        Command(
            "voice",
            ("голос",),
            "",
            "Голосовой ввод: надиктовать сообщение",
            hotkey="Ctrl+T",
        ),
        Command(
            "export",
            ("экспорт",),
            "[txt|json|log-txt|log-json]",
            "Экспорт сессии",
            arg_options=(
                ("txt", "Вся сессия в читаемом виде"),
                ("json", "Структурированные данные сессии"),
                ("log-txt", "Лог-файл агента, текущий запуск (.log)"),
                ("log-json", "JSONL-файл треда (вся сессия)"),
            ),
        ),
        Command(
            "copy",
            ("копировать", "копия"),
            "[N]",
            "Скопировать последний ответ агента или N-й с конца (/copy N)",
        ),
        Command(
            "compact",
            ("summarize", "сжать"),
            "",
            "Сжать контекст сессии сейчас",
        ),
        Command("init", ("инит",), "", "Анализ проекта и генерация AGENTS.md"),
        Command("skills", ("навыки", "скиллы"), "", "Обзор навыков"),
        Command("skill", ("навык", "скилл"), "<id>", "Использовать навык"),
        Command("status", ("статус",), "", "Состояние текущей сессии"),
        Command(
            "stop",
            ("cancel", "стоп", "отмена"),
            "",
            "Остановить активную задачу",
            hotkey="Ctrl+C",
        ),
        Command("mcp", ("мсп",), "", "MCP-серверы и их тулы"),
        Command(
            "buffer",
            ("буфер",),
            "",
            "Буфер сессии — текст собранных копий",
            hotkey="F7",
        ),
        Command("quit", ("exit", "выход", "выйти"), "", "Выход", hotkey="Esc"),
        # Переключатели палитры (←/→): самопроверка хода и формат финала.
        Command(
            "rigor",
            ("строгость",),
            "",
            _cap(f"{TOGGLES['rigor'].label}: {TOGGLES['rigor'].summary}"),
            is_toggle=True,
        ),
        Command(
            "gate",
            ("самопроверка", "гейт"),
            "",
            _cap(f"{TOGGLES['gate'].label}: {TOGGLES['gate'].summary}"),
            is_toggle=True,
        ),
        Command(
            "answers",
            ("формат",),
            "",
            _cap(f"{TOGGLES['answers'].label}: {TOGGLES['answers'].summary}"),
            is_toggle=True,
        ),
        Command(
            "reasoning",
            ("рассуждения",),
            "",
            _cap(TOGGLES["reasoning"].label),
            is_toggle=True,
        ),
    ]
    for c in cmds:
        reg.register(c)
    return reg
