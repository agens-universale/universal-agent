"""Владелец режимов разрешений: какой режим действует и что он разрешает.

Слой политики. Защиты (запись / чтение / команды) спрашивают здесь исход для
своей зоны; гейт диспатча (``gc_core.tools``) — исход для категорий без своей
защиты. Модуль не знает ни про UI, ни про транспорт: на вход — контекст рана,
на выход — исход.

Порядок слоёв, сверху вниз:
  0. полы: защищённые пути (свой модуль);
  1. запомненные ответы человека (``PermissionStore``): запрет «в сессии»
     сильнее режима и читается здесь (``session_deny``), разрешение — в
     confirm-порте, там, где иначе был бы задан вопрос;
  2. явные правила конфига (``security.<guard>.mode``, заданный пользователем);
  3. дефолт текущего режима — таблицы ниже.
"""

from __future__ import annotations

import json
import ntpath
import os
import posixpath
import re
import shlex
import sys
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from gc_core.constants.tool_names import PROPOSE_PLAN_TOOL_NAME
from gc_core.settings import is_explicit, settings_of


class Mode(StrEnum):
    EDIT = "edit"
    MANUAL = "manual"
    PLAN = "plan"
    AUTO = "auto"


DEFAULT_MODE = Mode.EDIT

#: Собранные режимы; порядок — цикл Shift+Tab. Чего здесь нет, нельзя ни
#: выбрать хоткеем, ни включить конфигом или флагом.
AVAILABLE_MODES: tuple[Mode, ...] = (
    Mode.EDIT,
    Mode.MANUAL,
    Mode.PLAN,
    Mode.AUTO,
)

#: Режимы, в которых исполняется одобренный план, — в порядке выбора у
#: человека: номера вариантов одобрения в TUI и консоли идут отсюда.
PLAN_EXECUTION_MODES: tuple[Mode, ...] = (Mode.EDIT, Mode.AUTO, Mode.MANUAL)

#: Имя режима для человека — одно на весь продукт (пилюля, ``/config``, доки).
MODE_LABELS: dict[str, str] = {
    Mode.EDIT.value: "edit mode",
    Mode.MANUAL.value: "manual mode",
    Mode.PLAN.value: "plan mode",
    Mode.AUTO.value: "auto mode",
}

# Исходы решения.
ALLOW = "allow"
ASK = "ask"
DENY = "deny"
JUDGE = "judge"  # решает судья команд (существующий путь модерации)
#: Решает судья действий режима auto: опасно ли действие и разрешил ли его
#: пользователь. Стоит там, где другие режимы спрашивают человека.
ALIGN = "align"
PASS = "pass"  # «не моё дело»: решает защита этой зоны

# Категории тулов. Первые четыре имеют собственные защиты, остальные решает гейт.
CAT_READ = "read"  # без побочных эффектов: чтение, вычисление, ожидание
CAT_EDIT = "edit"  # запись в файловую систему
CAT_EXEC = "exec"  # исполнение произвольных команд / кода
CAT_NETWORK = "network"  # исходящие запросы
CAT_STATE = "state"  # только внутреннее состояние агента (память сессии)
CAT_TURN = "turn"  # управление ходом
CAT_SUBAGENT = "subagent"  # запуск субагента (наследует режим родителя)
CAT_MCP = "mcp"
CAT_SKILL = "skill"  # чтение скилла: инструкции, референсы, ассеты
#: Запуск скрипта скилла: произвольный код мимо модерации команд. Своей
#: защиты у него нет, поэтому решает гейт — по строке действия, а не чтения.
CAT_SKILL_EXEC = "skill_exec"
#: Запись в постоянное состояние агента, переживающее сессию (память проекта
#: попадает в промпт следующих сессий). Категория не тула, а действия: её
#: спрашивает сам тул для записывающих действий, чтение остаётся CAT_STATE.
CAT_PERSISTENT_STATE = "persistent_state"
CAT_UNKNOWN = "unknown"

GUARD_CATEGORIES = frozenset({CAT_READ, CAT_EDIT, CAT_EXEC, CAT_NETWORK})

#: Команды, исполняемые без судьи и без вопроса в любом режиме. В списке только
#: то, что НЕ показывает содержимое файлов и НЕ умеет писать: имена, размеры,
#: состояние. У команды, где флаг читает файл (`wc`, `du`), признаются только
#: флаги из ``_FLAG_RULES``. Намеренно исключены:
#:   - cat / type / head / tail / grep, git diff / show / log — печатают
#:     содержимое: через них читается `.env` или ключ в обход защиты чтения,
#:     и надёжно отсечь это по аргументам нельзя (`cat .env*`, `grep -r KEY .`,
#:     `git show HEAD:.env`);
#:   - git diff / show / log и tree — пишут файл флагом вывода
#:     (`--output=<file>`, `-o <file>`);
#:   - find (мутирует под -delete/-exec), date (мутирует под -s).
#: Исключённое идёт обычным путём: судья команд либо вопрос человеку.
READONLY_COMMANDS: frozenset[str] = frozenset(
    {
        "ls", "dir", "pwd", "stat", "du", "df", "wc",
        "which", "where", "whoami", "echo",
    }
)  # fmt: skip

#: Пары «команда + подкоманда», безопасные с любыми аргументами, если у пары
#: нет таблицы флагов (``_FLAG_RULES``). git исполняет конфиг и хуки
#: репозитория (`core.fsmonitor`), поэтому `.git` — защищённый путь (запись
#: спрашивается), а git в другом каталоге безобидным не считается.
READONLY_PAIRS: frozenset[str] = frozenset({"git status", "git rev-parse"})

#: Пары, безопасные ТОЛЬКО без дополнительных аргументов: с аргументом они
#: мутируют (`git branch <name>` создаёт ветку, `git remote add …` меняет remote).
READONLY_BARE_PAIRS: frozenset[str] = frozenset({"git branch", "git remote"})

#: Первые слова пар: однословная запись `readonly_commands` с таким словом —
#: любая подкоманда (`git` — это и `git push`). Конфиг такую запись отвергает.
READONLY_PAIR_HEADS: frozenset[str] = frozenset(
    pair.split()[0] for pair in READONLY_PAIRS | READONLY_BARE_PAIRS
)

#: Оболочки и интерпретаторы: однословной записью `readonly_commands` они
#: сделали бы безобидным любой код (`python -c …`). Конфиг такую запись
#: отвергает. Список эталона и варианты Python (`pythonw`, `pypy`, `ipython`,
#: лаунчер `py`). Имена всех списков здесь — в том виде, в каком их
#: сравнивает ``command_name_variants``: нижний регистр, без `.exe`
#: (`cmd.exe` эталона — это `cmd`).
INTERPRETER_COMMANDS: frozenset[str] = frozenset(
    {
        "sh", "bash", "zsh", "dash", "ksh", "fish", "cmd",
        "powershell", "pwsh", "python", "python3", "node", "bun", "deno",
        "ruby", "perl", "php", "lua", "julia", "rscript", "env",
        "pythonw", "pypy", "ipython", "py",
    }
)  # fmt: skip

#: Запускатели: исполняют команду, переданную им аргументом (`sudo rm …`,
#: `xargs sh -c …`, `npx пакет`). Однословная запись `readonly_commands` с
#: ними открыла бы любую команду — конфиг такую запись отвергает. Список
#: неполон: запись в `readonly_commands` — выбор пользователя.
COMMAND_LAUNCHERS: frozenset[str] = frozenset(
    {
        "sudo", "doas", "su", "xargs", "npx", "pnpx", "bunx", "uvx", "pipx",
        "timeout", "nice", "nohup", "exec", "eval", "command", "time",
        "watch", "strace", "stdbuf", "setsid", "ionice", "chroot", "flock",
        "nsenter", "unshare", "runuser", "pkexec", "busybox", "parallel",
        "script", "ssh", "runas", "wsl",
    }
)  # fmt: skip

#: Подкоманды, исполняющие произвольный код (`uv run`, `npm exec`,
#: `pnpm dlx`, `docker exec`): пара с ними в `readonly_commands` отвергается.
RUNNER_SUBCOMMANDS: frozenset[str] = frozenset({"run", "exec", "dlx", "x"})

#: Команды, у которых есть такая подкоманда: одним словом (`uv`) запись
#: открыла бы и `uv run …` — шире пары, которую конфиг отвергает. Такой
#: записи нужна подкоманда (`npm ls`, `cargo tree`). Список неполон.
RUNNER_TOOLS: frozenset[str] = frozenset(
    {
        "uv", "npm", "pnpm", "yarn", "docker", "podman", "kubectl", "cargo",
        "poetry", "pipenv", "pdm", "hatch", "go", "dotnet", "gradle", "mvn",
    }
)  # fmt: skip


def command_name_variants(word: str) -> set[str]:
    """Имя команды так, как его сравнивают списки выше (в нижнем регистре):
    без пути, без `.exe`, без регистра (Windows) и без хвоста версии и
    варианта сборки (`/usr/bin/python3.13t` → `python3.13t`, `python`;
    `python3.12-dbg` → `python`). Прочий `-хвост` остаётся: `python3-config`
    и `php8.2-fpm` — не интерпретаторы."""
    name = re.split(r"[\\/]", word)[-1].lower()
    name = name.removesuffix(".exe")
    return {name, re.sub(r"[\d.]+[a-z]?(-dbg|-debug)?$", "", name)}


def interpreter_named(word: str) -> bool:
    """Первое слово записи — оболочка или интерпретатор."""
    return bool(command_name_variants(word) & INTERPRETER_COMMANDS)


def launcher_named(word: str) -> bool:
    """Первое слово записи запускает другую команду."""
    return bool(command_name_variants(word) & COMMAND_LAUNCHERS)


def pair_head_named(word: str) -> bool:
    """Первое слово — команда, которой нужна подкоманда: из таблицы
    встроенных пар (`git`) или с подкомандой запуска кода (`uv`, `npm`)."""
    return bool(
        command_name_variants(word) & (READONLY_PAIR_HEADS | RUNNER_TOOLS)
    )


@dataclass(frozen=True)
class FlagRule:
    """Разрешённые флаги встроенной безобидной команды — как у эталона:
    «каждый флаг — только чтение». Флаг вне таблицы, в том числе сокращение
    разрешённого длинного (GNU и git принимают однозначный префикс:
    `--files0` = `--files0-from`), делает команду «не безобидной» — дальше
    обычный путь режима. Позиционные аргументы (пути) разрешены; после `--`
    всё позиционное.

    ``short`` — короткие без значения, в том числе слитно (`-lw`);
    ``short_value`` — короткие со значением: остаток слитка — значение
    (`-d1`, `-uno`); ``long`` — полные имена ровно, значение — через `=`.
    """

    short: str = ""
    short_value: str = ""
    long: frozenset[str] = frozenset()

    def allows(self, args: list[str]) -> bool:
        for arg in args:
            if arg == "--":
                return True
            if not self._allows_one(arg):
                return False
        return True

    def _allows_one(self, arg: str) -> bool:
        if arg.startswith("--"):
            return arg.partition("=")[0] in self.long
        if arg == "-" or not arg.startswith("-"):
            return True  # путь или `-` (стандартный ввод)
        for flag in arg[1:]:
            if flag in self.short_value:
                return True  # остаток слитка — значение флага
            if flag not in self.short:
                return False
        return True


#: Таблицы флагов встроенных команд, у которых есть флаг, читающий файл или
#: печатающий содержимое (аудит — спека §5.4): `wc`/`du --files0-from`,
#: `du -X/--exclude-from`, `git status -v` (печатает дифф). У остальных
#: встроенных таких флагов нет — они признаются с любыми аргументами.
_FLAG_RULES: dict[str, FlagRule] = {
    "wc": FlagRule(
        short="lwcmL",
        long=frozenset(
            {"--lines", "--words", "--bytes", "--chars", "--max-line-length"}
        ),
    ),
    "du": FlagRule(
        short="shackmbx",
        short_value="d",
        long=frozenset(
            {
                "--summarize", "--human-readable", "--all", "--total",
                "--apparent-size", "--one-file-system", "--max-depth",
            }
        ),
    ),
    "git status": FlagRule(
        short="sbz",
        short_value="uM",
        long=frozenset(
            {
                "--short", "--branch", "--show-stash", "--porcelain",
                "--long", "--null", "--untracked-files", "--ignored",
                "--ignore-submodules", "--column", "--no-column",
                "--ahead-behind", "--no-ahead-behind", "--renames",
                "--no-renames", "--find-renames",
            }
        ),
    ),
}  # fmt: skip


#: Метасимволы shell: их наличие означает составную команду, а составная
#: команда read-only не бывает — «ls; rm -rf build» начинается с «ls».
#: `$` — любая подстановка: и `$(…)`, и `$VAR` (`echo $GITHUB_TOKEN` печатает
#: секрет). Включены и Windows-специфичные: PowerShell @(...), cmd.exe %VAR%
#: и !VAR!.
_SHELL_METACHARS = (
    ";", "|", "&", ">", "<", "`", "$", "\n", "\r", "@(", "%", "!",
)  # fmt: skip


@dataclass(frozen=True)
class PerimeterProfile:
    """Исходы для зоны, у которой есть периметр проекта.

    ``outside == PASS`` означает «режим ничего не добавляет»: решение принимает
    сама защита зоны своей веткой нарушения периметра — она же применяет явный
    ключ ``<guard>.mode`` и формирует конверт со списком разрешённых корней.
    Дублировать эти клетки в резолвере значило бы получить тот же исход с
    другим, менее информативным конвертом.
    """

    inside: str
    outside: str
    #: Вопрос режима вне периметра держится и при явном ``allow`` защиты: режим
    #: выбран в сессии сознательно, а ``allow`` в конфиге — давнее общее.
    firm_question: bool = False

    @property
    def adds_nothing(self) -> bool:
        """Режим не меняет поведение защиты: внутри можно, снаружи решает она.
        Тогда стадии режима незачем выяснять, по какую сторону периметра путь."""
        return self.inside == ALLOW and self.outside == PASS


#: Запреты plan (``DENY``) — пол: агент в этом режиме только исследует. Их не
#: снимает ни запомненный ответ человека, ни явный ``security.<guard>.mode``.
_WRITE_PROFILES: dict[Mode, PerimeterProfile] = {
    Mode.MANUAL: PerimeterProfile(ASK, ASK, firm_question=True),
    Mode.EDIT: PerimeterProfile(ALLOW, PASS),
    Mode.PLAN: PerimeterProfile(DENY, DENY),
    Mode.AUTO: PerimeterProfile(ALLOW, ALIGN),
}

#: Чтение. MANUAL вне корней решает сам (иначе ``read_guard.enabled: false``
#: молча отменял бы режим); EDIT вне корней отдаёт решение защите.
_READ_PROFILES: dict[Mode, PerimeterProfile] = {
    Mode.MANUAL: PerimeterProfile(ALLOW, ASK, firm_question=True),
    Mode.EDIT: PerimeterProfile(ALLOW, PASS),
    Mode.PLAN: PerimeterProfile(ALLOW, ASK),
    Mode.AUTO: PerimeterProfile(ALLOW, ALIGN),
}

#: Команды вне списка только-чтения.
_EXEC_PROFILES: dict[Mode, str] = {
    Mode.MANUAL: ASK,
    Mode.EDIT: JUDGE,
    Mode.PLAN: DENY,
    Mode.AUTO: ALIGN,
}

#: Сетевой запрос на адрес, прошедший политику адресов. EDIT и PLAN ничего не
#: добавляют (прежнее поведение); MANUAL спрашивает — запрос уходит наружу;
#: AUTO отдаёт судье.
_NETWORK_PROFILES: dict[Mode, str] = {
    Mode.MANUAL: ASK,
    Mode.EDIT: PASS,
    Mode.PLAN: PASS,
    Mode.AUTO: ALIGN,
}

_ALWAYS_ALLOWED: dict[Mode, str] = dict.fromkeys(Mode, ALLOW)

_CATEGORY_TABLE: dict[str, dict[Mode, str]] = {
    CAT_MCP: {
        Mode.MANUAL: ASK,
        Mode.EDIT: ALLOW,
        Mode.PLAN: DENY,
        Mode.AUTO: ALIGN,
    },
    CAT_SKILL: _ALWAYS_ALLOWED,
    # edit — прежнее поведение: скрипт скилла запускался молча.
    CAT_SKILL_EXEC: {
        Mode.MANUAL: ASK,
        Mode.EDIT: ALLOW,
        Mode.PLAN: DENY,
        Mode.AUTO: ALIGN,
    },
    CAT_STATE: _ALWAYS_ALLOWED,
    # Собственная память агента пишется без вопроса в edit и auto (как у
    # эталона), но она попадает в промпт следующих сессий: manual её
    # спрашивает, plan обещает только чтение — там это изменение.
    CAT_PERSISTENT_STATE: {
        **_ALWAYS_ALLOWED,
        Mode.MANUAL: ASK,
        Mode.PLAN: DENY,
    },
    CAT_TURN: _ALWAYS_ALLOWED,
    # Запуск субагента в auto проверяется: его задание — выбор агента. В
    # остальных режимах спрашиваются действия самого субагента, а не запуск.
    CAT_SUBAGENT: {**_ALWAYS_ALLOWED, Mode.AUTO: ALIGN},
    # Тул без разметки. В edit — прежнее поведение (режим по умолчанию ничего
    # не меняет ни для одного тула), в manual — спросить. Сам факт «тул без
    # категории» ловится не здесь, а сторожем реестров
    # (tests/tools/test_tool_categories.py): в сборке таких тулов нет.
    CAT_UNKNOWN: {
        Mode.MANUAL: ASK,
        Mode.EDIT: ALLOW,
        Mode.PLAN: DENY,
        Mode.AUTO: ALIGN,
    },
}


def mode_label(mode: object) -> str:
    """Имя режима для человека; незнакомое значение возвращается как есть."""
    value = mode.value if isinstance(mode, Mode) else str(mode or "")
    return MODE_LABELS.get(value, value)


def _command_tokens(command: str) -> list[str]:
    """Слова команды для списка безобидных; пусто — не безобидна.

    Fail-closed: пустая строка, ошибка разбора или любой shell-метасимвол.
    """
    text = str(command or "").strip()
    if not text or any(marker in text for marker in _SHELL_METACHARS):
        return []
    try:
        return shlex.split(text)
    except ValueError:
        return []


def _flags_allowed(command: str, args: list[str]) -> bool:
    rule = _FLAG_RULES.get(command)
    return rule is None or rule.allows(args)


def _readonly_entries(extra: list[str] | None) -> set[str]:
    """Записи `readonly_commands`: команда или пара «команда подкоманда»;
    пробелы внутри записи схлопываются."""
    return {" ".join(str(item).split()) for item in extra or ()} - {""}


def is_readonly_command(command: str, extra: list[str] | None = None) -> bool:
    """Признать команду безобидной: исполнять без судьи и без подтверждения.

    Встроенная команда — только с флагами своей таблицы (``_FLAG_RULES``);
    дополнение из конфига её не ослабляет. Дополнение пользователя — с
    любыми аргументами: это его выбор. Запись дополнения — команда или пара
    «команда подкоманда» (`git log`), обе — с любыми аргументами. Остальное
    решает режим.
    """
    tokens = _command_tokens(command)
    if not tokens:
        return False
    head, pair = tokens[0], " ".join(tokens[:2])
    if head in READONLY_COMMANDS:
        return _flags_allowed(head, tokens[1:])
    if pair in READONLY_PAIRS:
        return _flags_allowed(pair, tokens[2:])
    if pair in READONLY_BARE_PAIRS and len(tokens) == 2:
        return True
    entries = _readonly_entries(extra)
    return head in entries or pair in entries


#: Ключ записи реестра: тул предлагается модели только в названном режиме.
#: В общий список схем (и в системный промпт сессии) такой тул не входит —
#: его добавляет ``tools_for_mode`` на запросе. Диспетчер исполнит его в любом
#: режиме: вне своего режима тул сам отвечает отказом.
OFFERED_IN_MODE_KEY = "offered_in_mode"


def _offered_in_mode(ctx: Any, state: Any, mode: str) -> list[dict]:
    """Схемы тулов, которые предлагаются модели только в ``mode``. Тул
    режима видит только корневой агент."""
    if getattr(state, "parent_state_id", None) is not None:
        return []
    entries = getattr(ctx, "tools_data", None) or {}
    return [
        entry["spec"]
        for entry in entries.values()
        if isinstance(entry, dict)
        and entry.get(OFFERED_IN_MODE_KEY) == mode
        and isinstance(entry.get("spec"), dict)
    ]


def tools_for_mode(
    tools: list[dict], ctx: Any, state: Any = None
) -> list[dict]:
    """Набор тулов на запрос к модели: общий список плюс тулы текущего режима.

    Тул режима (``turn_propose_plan`` в plan) видит только корневой агент. В
    режиме по умолчанию список не меняется — запросы остаются прежними.
    """
    return [*tools, *_offered_in_mode(ctx, state, resolve_mode(ctx).value)]


def offers_plan_tool(ctx: Any, state: Any = None) -> bool:
    """Есть ли у ЭТОГО агента в plan тул плана — по его набору тулов, тем
    же правилом, что ``tools_for_mode``. От этого зависит, что агенту
    говорится о plan: «предложи план» или «верни находки текстом»."""
    return any(
        spec.get("name") == PROPOSE_PLAN_TOOL_NAME
        for spec in _offered_in_mode(ctx, state, Mode.PLAN.value)
    )


def coerce_mode(raw: object) -> Mode | None:
    """Строка → режим, если он собран; иначе None."""
    if isinstance(raw, Mode):
        return raw if raw in AVAILABLE_MODES else None
    text = str(raw or "").strip().lower()
    for mode in AVAILABLE_MODES:
        if mode.value == text:
            return mode
    return None


def next_mode(mode: Mode) -> Mode:
    """Следующий режим цикла (Shift+Tab), только среди собранных."""
    order = AVAILABLE_MODES
    try:
        idx = order.index(mode)
    except ValueError:
        return order[0]
    return order[(idx + 1) % len(order)]


def _store(ctx: Any) -> Any:
    return getattr(ctx, "permission_store", None)


def resolve_mode_with_source(ctx: Any) -> tuple[Mode, str]:
    """Действующий режим и его источник: 'session' | 'config' | 'default'.

    'session' — выбор, живущий в состоянии сессии: его ставит Shift+Tab, а на
    старте прогона — флаг запуска (исполнитель кладёт его поверх сохранённого).
    """
    store = _store(ctx)
    getter = getattr(store, "permission_mode", None)
    return mode_with_source(
        getter() if callable(getter) else None, settings_of(ctx)
    )


def mode_with_source(
    session_choice: object, settings: Any
) -> tuple[Mode, str]:
    """Правило приоритета режима — одно на движок и витрины: выбор сессии →
    конфиг → встроенный. Витрина зовёт его со своим выбором, пока контекста
    прогона ещё нет."""
    mode = coerce_mode(session_choice)
    if mode is not None:
        return mode, "session"
    mode = coerce_mode(settings.security.permissions.default_mode)
    if mode is None:
        return DEFAULT_MODE, "default"
    explicit = is_explicit(settings, "security.permissions.default_mode")
    return mode, ("config" if explicit else "default")


def resolve_mode(ctx: Any) -> Mode:
    return resolve_mode_with_source(ctx)[0]


#: Режимы, которые без человека не работают: в plan план некому одобрить, в
#: manual подтверждать некому. Каналы без человека (gc-serve, gc-a2a)
#: предупреждают о них при старте; старт не прерывается.
_HEADLESS_UNSUPPORTED_MODES = frozenset({Mode.PLAN, Mode.MANUAL})


def headless_mode_warning(settings: Any) -> str | None:
    """Режим по умолчанию, который в канале без человека не работает: его
    ключ (``plan`` / ``manual``) или None.

    Своего выбора режима у сессий такого канала нет — действует
    ``security.permissions.default_mode``. Текст —
    ``message_policy.mode_messages.headless_mode_warning_text``.
    """
    mode, _source = mode_with_source(None, settings_of(settings))
    return mode.value if mode in _HEADLESS_UNSUPPORTED_MODES else None


def guard_mode_key(guard: str) -> str:
    """Путь ключа конфига ``security.<защита>.mode``.

    Склейкой, а не f-строкой: сетка строковых ссылок на модули
    (``gc_testkit.module_literals``) читает f-строку, начатую с
    ``security.``, как ссылку на старый модуль ``security``.
    """
    return ".".join(("security", guard, "mode"))


def explicit_guard_mode(ctx: Any, guard: str) -> str | None:
    """Режим защиты, ЗАДАННЫЙ пользователем явно (слой 2). None — молчал."""
    settings = settings_of(ctx)
    if not is_explicit(settings, guard_mode_key(guard)):
        return None
    value = str(getattr(settings.security, guard).mode or "").strip().lower()
    return value if value in (ALLOW, ASK, DENY) else None


def _perimeter_decision(
    ctx: Any, profile: PerimeterProfile, guard: str, *, inside: bool
) -> str:
    """Исход зоны с периметром: режим решает и внутри, и снаружи.

    Вне периметра явный ключ ``security.<guard>.mode`` (слой 2) сильнее дефолта
    режима (слой 3) — исторически этот ключ описывает именно нарушение
    периметра. Если профиль отдаёт зону защите (``PASS``), явный ключ тоже
    остаётся ей: она применит его сама, вместе со своим конвертом.
    """
    if inside:
        return profile.inside
    if profile.outside in (PASS, DENY):
        # PASS — зона отдана защите; DENY — пол режима, явный ключ его не снимает.
        return profile.outside
    explicit = explicit_guard_mode(ctx, guard)
    if explicit is None or (explicit == ASK and profile.outside == ALIGN):
        # `ask` не подменяет судью действий: значение стоит в поставляемом
        # конфиге, и auto вёл бы себя как manual в каждой установке.
        # `allow` и `deny` сильнее судьи.
        return profile.outside
    if explicit == ALLOW and profile.firm_question:
        # `deny` сильнее и здесь; в остальных профилях `allow` — как было:
        # снимает вопрос периметра или заменяет судью.
        return profile.outside
    return explicit


def write_profile(ctx: Any) -> PerimeterProfile:
    return _WRITE_PROFILES[resolve_mode(ctx)]


def read_profile(ctx: Any) -> PerimeterProfile:
    return _READ_PROFILES[resolve_mode(ctx)]


def write_decision(ctx: Any, *, inside: bool) -> str:
    """Исход записи. ``inside`` — путь внутри периметра проекта."""
    return _perimeter_decision(
        ctx, _WRITE_PROFILES[resolve_mode(ctx)], "write_guard", inside=inside
    )


def read_decision(ctx: Any, *, inside: bool) -> str:
    return _perimeter_decision(
        ctx, _READ_PROFILES[resolve_mode(ctx)], "read_guard", inside=inside
    )


def exec_decision(ctx: Any, *, readonly: bool) -> str:
    """Исход команды. ``readonly`` — команда из списка только-чтения.

    Ветки явного ключа конфига здесь нет: ``command_moderation.mode`` —
    политика эскалации вердикта судьи команд («зови судью, и если он сказал
    опасно — спроси человека»), а не готовый исход. Шаблон конфига задаёт
    этот ключ явно, и подстановка его как исхода перекрывала бы таблицу
    режимов в каждой установке. В auto и явный `allow` судью действий не
    снимает: как у эталона, широкие правила произвольного исполнения в auto
    не действуют.
    """
    if readonly:
        return ALLOW
    return _EXEC_PROFILES[resolve_mode(ctx)]


def floor_question_decision(outcomes: tuple[str, ...]) -> str:
    """Кто отвечает на вопрос пола защищённых путей (файл инструкций):
    человек (``ASK``), судья действий (``ALIGN``) — там, где режим отдаёт
    судье всё, о чём иначе спросили бы человека, — или никто (``PASS``): режим,
    запрещающий действие сам (plan), решит без вопроса, и ответ человека ничего
    бы не изменил.

    ``outcomes`` — исходы режима для этого вида действия: внутри и снаружи
    проекта у записи, исход команды у команд. Один источник истины — профиль
    этого действия.
    """
    if ALIGN in outcomes:
        return ALIGN  # режим отдаёт такие действия судье
    if all(outcome == DENY for outcome in outcomes):
        return PASS
    return ASK


def network_decision(ctx: Any) -> str:
    """Исход режима для сетевого запроса; ``PASS`` — режим ничего не добавляет."""
    return _NETWORK_PROFILES[resolve_mode(ctx)]


def follows_cross_host_redirects(ctx: Any) -> bool:
    """Идти ли по редиректу на ДРУГОЙ хост.

    Режим, проверяющий сетевые запросы (вопрос в manual, судья в auto),
    проверил только названный адрес: переход на другой хост — новый запрос,
    модель делает его отдельно, и он проходит ту же проверку. Где режим
    ничего не добавляет (edit, plan), — как раньше. Переходы в пределах
    хоста идут всегда.
    """
    return network_decision(ctx) == PASS


def readonly_extra(ctx: Any) -> list[str]:
    """Пользовательское дополнение списка только-чтения из конфига."""
    return list(settings_of(ctx).security.permissions.readonly_commands or [])


def category_decision(category: str, ctx: Any) -> str:
    """Исход для категории без собственной защиты; у остальных — PASS."""
    if category in GUARD_CATEGORIES:
        return PASS
    row = _CATEGORY_TABLE.get(category) or _CATEGORY_TABLE[CAT_UNKNOWN]
    return row[resolve_mode(ctx)]


# --- запомненный запрет и ключи решений ---------------------------------------


def session_deny(ctx: Any, decision_key: str | None) -> bool:
    """Человек запретил это действие «в сессии» (ответ N).

    Такой запрет сильнее режима: проверяется раньше его разрешения. Читаются
    ТОЛЬКО session-правила: «запретить разово» (T) живёт в run-правилах и
    относится к одному вызову — на другие режимы и вызовы он не переносится.
    """
    if not decision_key:
        return False
    store = _store(ctx)
    rules = getattr(store, "session_rules", None)
    if not callable(rules):
        return False
    return rules().get(decision_key) is False


#: Модуль путей ОС — не `os.path`: стадии режима исполняются и внутри workflow
#: Temporal, где песочница запрещает `os.path` и вызовы `os`. `posixpath` и
#: `ntpath` — модули стандартной библиотеки, они проходят в песочницу насквозь.
PATHS = ntpath if sys.platform == "win32" else posixpath


def _as_text(path: Any) -> str:
    """`str` или путь-объект — без `os.fspath` (вызов `os` в песочнице)."""
    if isinstance(path, str):
        return path
    return str(path.__fspath__()) if hasattr(path, "__fspath__") else str(path)


def decision_path_key(
    prefix: str, path: str | os.PathLike[str], base_dir: Any = None
) -> str:
    """Ключ решения для пути — единого вида: ``a.py`` и ``./a.py`` дают один ключ.

    Приведение чисто строковое (без обращения к диску): файловая система рана
    может быть не локальной, а ключ должен строиться одинаково везде. `~` и
    переменные окружения раскрываются: `~/.ssh/id_rsa` и `$HOME/.ssh/id_rsa` —
    один ключ (символические ссылки так не свести — это нужна файловая
    система).
    """
    raw = _as_text(path)
    raw = PATHS.expandvars(PATHS.expanduser(raw)) if raw else raw
    if raw and not PATHS.isabs(raw) and base_dir:
        raw = PATHS.join(_as_text(base_dir), raw)
    return f"{prefix}:{PATHS.normcase(PATHS.normpath(raw)) if raw else raw}"


def decision_identity(entry: dict, arguments: Any) -> list[tuple[str, str]]:
    """Аргументы, которыми тул объявил себя различимым: ``entry['decision_args']``.

    У команды ключ решения — её текст, у записи — путь. Тул без такого ключа
    получил бы самый грубый ключ («имя тула»), и одно «разрешить» открывало бы
    все последующие вызовы — для произвольного исполнения это отменяет смысл
    manual. Состав различающих аргументов знает реестр тула, а не резолвер:
    здесь нет ни одной ветки «если тул такой-то».

    Значения берутся из СЫРЫХ аргументов, поэтому неразобранный вход даёт «?»
    — ключ остаётся отличимым от ключа успешно опознанного вызова.
    """
    names = tuple((entry or {}).get("decision_args") or ())
    if not names:
        return []
    data = arguments
    if isinstance(data, (str, bytes, bytearray)):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            data = None
    if not isinstance(data, dict):
        data = {}
    return [
        (str(name), str(data.get(name) or "").strip() or "?") for name in names
    ]


#: Ключ записи реестра: категория по значению аргумента ``action`` — у тула,
#: чьи действия разного рода (память проекта: чтение — состояние, запись —
#: постоянное состояние).
ACTION_CATEGORIES_KEY = "action_categories"


def call_category(entry: dict | None, arguments: Any = None) -> str:
    """Категория ЭТОГО вызова: по действию, если тул их различает, иначе тула."""
    entry = entry or {}
    by_action = entry.get(ACTION_CATEGORIES_KEY) or {}
    action = arguments.get("action") if isinstance(arguments, dict) else None
    return str(by_action.get(action) or entry.get("category") or CAT_UNKNOWN)


def gate_decision_key(
    tool_name: str, entry: dict, arguments: Any = None
) -> str:
    """Ключ решения для действия, проходящего через гейт диспатча."""
    category = call_category(entry, arguments)
    if category == CAT_MCP:
        server = str((entry or {}).get("mcp_server") or "").strip()
        return f"mcp:{server}:{tool_name}" if server else f"mcp:{tool_name}"
    suffix = ":".join(
        value for _, value in decision_identity(entry or {}, arguments)
    )
    return f"{category}:{tool_name}" + (f":{suffix}" if suffix else "")
