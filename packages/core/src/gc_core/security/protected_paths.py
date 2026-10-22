from __future__ import annotations

import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gc_core.attachments.store import STAGING_DIRNAME
from gc_core.config_modules.env_vars import gc_paths, project_gc_dir
from gc_core.instructions.layers import instruction_file_paths
from gc_core.security.path_policy import is_within_root
from gc_core.settings import settings_of

#: Заголовок вопроса о защищённом пути — одинаковый для записи и команды:
#: человек должен видеть, почему спрашивают даже там, где запись свободна.
PROTECTED_PATH_TITLE = "Защищённый путь"


@dataclass(frozen=True)
class ProtectedSet:
    instructions: frozenset[Path]
    operational: frozenset[Path]
    skills_exceptions: frozenset[Path]
    #: Рабочая область рана, лежащая ВНУТРИ служебного корня gc (sandbox
    #: ``workdir: auto`` → ``<state>/sandboxes/<slug>``). Сама она не
    #: служебная — иначе каждая команда в ней ловила бы отказ; служебными
    #: остаются её ``.gc`` и ``extra`` (``operational_hard``).
    workspace_exception: Path | None = None
    operational_hard: frozenset[Path] = frozenset()
    # Корни класса `media` (`security.media_roots`): чтение — да, запись тулами —
    # нет, команды — с подтверждением. Пусто — класса нет: так набор собирают
    # вызывающие без конфига (судья команд, diff-TUI).
    media: frozenset[Path] = frozenset()
    #: Корень проекта набора (`cwd`): настройки инструментов внутри него ищутся
    #: только в частях пути ниже корня — сам корень может лежать в каталоге из
    #: списка (рабочая копия другого инструмента). None — все части.
    project: Path | None = None


def protected_paths(
    *,
    cwd: str | None = None,
    extra: list[str] | None = None,
    media: Iterable[str | os.PathLike[str]] = (),
) -> ProtectedSet:
    proj = Path(cwd) if cwd else Path(os.getcwd())
    paths = gc_paths()
    instr = set(instruction_file_paths(cwd=str(proj)).values())

    hard = {project_gc_dir(str(proj))}
    for raw in extra or []:
        hard.add(Path(os.path.abspath(raw)))
    gc_roots = {paths[key] for key in ("config", "state", "cache", "data")}
    ci = os.name == "nt"
    # Только каталог рана песочницы: cwd=~/.config/gc не снимает защиту с config.yml.
    sandboxes = paths["state"] / "sandboxes"
    workspace = (
        proj
        if is_within_root(str(proj), str(sandboxes), case_insensitive=ci)
        and proj != sandboxes
        else None
    )

    # skills_runtime — копия слоёв скиллов в среде исполнения remote-бэкенда
    # (tools/skills/provision.py): для модели это те же скиллы, что и
    # `.gc/skills`, иначе чтение скрипта скилла ловит protected_path_denied.
    # Запись в скиллы — вопрос пола (`_ASK_CLASSES`), чтение — свободно.
    skills = {
        project_gc_dir(str(proj)) / "skills",
        project_gc_dir(str(proj)) / "skills_runtime",
        paths["data"] / "skills",
    }

    return ProtectedSet(
        instructions=frozenset(instr),
        operational=frozenset(hard | gc_roots),
        skills_exceptions=frozenset(skills),
        workspace_exception=workspace,
        operational_hard=frozenset(hard),
        media=frozenset(
            Path(os.path.abspath(os.fspath(root))) for root in media
        ),
        project=Path(os.path.abspath(proj)),
    )


_VCS_DIR = ".git"
_CASE_INSENSITIVE_FS = os.name == "nt" or sys.platform == "darwin"


def _is_vcs(p: str) -> bool:
    """Путь внутри любого каталога `.git` — корня или вложенного репозитория:
    в нём конфиг и хуки, которые git исполняет сам (`core.fsmonitor` при
    `git status`, хуки при `git commit`). Регистр не различается везде: на
    Маке `.GIT` — тот же каталог; на различающих регистр системах лишняя
    защита одноимённого каталога безвредна."""
    return _VCS_DIR in (part.lower() for part in Path(p).parts)


#: Настройки инструментов: их исполняет другая программа мимо агента — хуки
#: git, профили оболочки, менеджеры пакетов, IDE, другие агенты (их хуки и
#: MCP-серверы запускаются сами). Состав — список эталона плюс один каталог
#: (решение владельца); своих агентов сверх этого не перечисляем — список
#: неполон по природе. Сравнение — по имени, не только в проекте: каталог —
#: по подряд идущим компонентам пути, файл — по имени; внутри проекта —
#: только части ниже его корня, вне — все. Строкой, без диска (стадии
#: режима исполняются и в коде workflow Temporal).
TOOL_CONFIG_DIRS: tuple[tuple[str, ...], ...] = (
    (".config", "git"),
    (".vscode",),
    (".idea",),
    (".husky",),
    (".cargo",),
    (".devcontainer",),
    (".yarn",),
    (".mvn",),
    (".claude",),
    (".codex",),
)
TOOL_CONFIG_FILES: frozenset[str] = frozenset(
    {
        ".gitconfig", ".gitmodules", ".bashrc", ".bash_profile",
        ".bash_login", ".bash_aliases", ".bash_logout", ".zshrc",
        ".zprofile", ".zshenv", ".zlogin", ".zlogout", ".profile",
        ".envrc", ".npmrc", ".yarnrc", ".yarnrc.yml", ".pnp.cjs",
        ".pnp.loader.mjs", ".pnpmfile.cjs", "bunfig.toml", ".bunfig.toml",
        ".bazelrc", ".bazelversion", ".bazeliskrc",
        ".pre-commit-config.yaml", "lefthook.yml", "lefthook.yaml",
        ".lefthook.yml", ".lefthook.yaml", "gradle-wrapper.properties",
        "maven-wrapper.properties", ".devcontainer.json", ".ripgreprc",
        "pyrightconfig.json", ".mcp.json", ".claude.json",
    }
)  # fmt: skip


def _is_tool_config(p: str, project: Path | None, ci: bool) -> bool:
    """Путь — файл настроек инструмента или лежит в их каталоге. В проекте
    смотрим только части ниже корня, вне проекта — все. Регистр — по
    правилу файловой системы; имена списка — в нижнем регистре."""
    parts = Path(p).parts
    if project is not None and is_within_root(p, project, case_insensitive=ci):
        parts = parts[len(project.parts) :]
    if ci:
        parts = tuple(part.lower() for part in parts)
    if parts and parts[-1] in TOOL_CONFIG_FILES:
        return True
    return any(
        parts[i : i + len(name)] == name
        for name in TOOL_CONFIG_DIRS
        for i in range(len(parts) - len(name) + 1)
    )


def _project_class(p: str, project: Path | None, ci: bool) -> str:
    """Класс пути вне служебных корней: каталог `.git`, настройки
    инструментов или обычный путь."""
    if _is_vcs(p):
        return "vcs"
    return "tool_config" if _is_tool_config(p, project, ci) else "none"


def _is_media(p: str, roots: frozenset[Path], ci: bool) -> bool:
    """Путь под корнем `media`, но не в кармане `_staging` этого корня.

    Карман хранилища картинок держит вставки ВСЕХ процессов проекта. В класс
    входит только собственный карман процесса — он приходит отдельным корнем
    (`media_roots`), и своего `_staging` у него нет. Чужие карманы —
    `_in_staging`.
    """
    return any(
        is_within_root(p, root, case_insensitive=ci)
        and not is_within_root(p, root / STAGING_DIRNAME, case_insensitive=ci)
        for root in roots
    )


def _in_staging(p: str, roots: frozenset[Path], ci: bool) -> bool:
    """Путь в кармане `_staging` корня `media` — вставки процессов до отправки."""
    return any(
        is_within_root(p, root / STAGING_DIRNAME, case_insensitive=ci)
        for root in roots
    )


def classify_path(path, ps: ProtectedSet) -> str:
    p = os.path.abspath(os.fspath(path))
    # Регистр не различают файловые системы Windows и по умолчанию macOS:
    # `.GC` там — тот же служебный каталог, `Agents.md` — тот же файл.
    ci = _CASE_INSENSITIVE_FS
    if any(
        is_within_root(p, r, case_insensitive=ci) for r in ps.skills_exceptions
    ):
        return "skills"
    if any(is_within_root(p, r, case_insensitive=ci) for r in ps.instructions):
        return "instructions"
    # Раньше operational: корень хранилища картинок по умолчанию лежит под <gc_state>.
    if _is_media(p, ps.media, ci):
        return "media"
    # Чужой карман (свой уже media выше) — operational, где бы ни лежал корень:
    # свой `threads_data_path` и временный корень SDK `<gc_state>` не накрывает.
    if _in_staging(p, ps.media, ci):
        return "operational"
    if any(
        is_within_root(p, r, case_insensitive=ci) for r in ps.operational_hard
    ):
        return "operational"
    if ps.workspace_exception is not None and is_within_root(
        p, ps.workspace_exception, case_insensitive=ci
    ):
        return _project_class(p, ps.project, ci)
    if any(is_within_root(p, r, case_insensitive=ci) for r in ps.operational):
        return "operational"
    # После служебных: `.git` и настройки инструментов под ними остаются
    # запретом, а не вопросом.
    return _project_class(p, ps.project, ci)


#: Классы с вопросом пола: запись в них меняет то, что потом исполнит или
#: прочтёт как указание кто-то мимо агента. Файлы инструкций и скиллы —
#: агент в следующих сессиях; `.git` — сам git (конфиг, хуки); настройки
#: инструментов — оболочка, менеджеры пакетов, IDE, другие агенты.
_ASK_CLASSES = frozenset({"instructions", "vcs", "skills", "tool_config"})


def protected_decision(path, *, cwd=None, extra=None, media=()) -> str:
    cls = classify_path(
        path, protected_paths(cwd=cwd, extra=extra, media=media)
    )
    if cls in _ASK_CLASSES:
        return "ask"
    if cls in ("operational", "media"):
        return "deny"
    return "allow"


def protected_extra_paths(source: Any) -> list[str]:
    """Дополнительные защищённые пути из ``security.protected_paths.extra``.

    Это ЗАПРЕТНЫЕ пути («нельзя читать/писать агенту»), а не разрешённые:
    ``write_guard.extra_paths`` — их противоположность (дополнительные каталоги
    для записи), и в набор защищённых им не место. ``~`` раскрывается здесь —
    ``protected_paths`` берёт только ``abspath``. ``source`` — то, что
    принимает ``settings_of``.
    """
    # ``expanduser`` — только для ``~``: guard команд считает это и в коде
    # durable-workflow, где обращение к ``os.path`` запрещено песочницей
    # (воркер раскрывает ``~`` профиля заранее).
    return [
        _expand_home(str(raw))
        for raw in settings_of(source).security.protected_paths.extra
        if str(raw).strip()
    ]


def _expand_home(raw: str) -> str:
    if not raw.startswith("~"):
        return raw
    try:
        return os.path.expanduser(raw)
    except Exception:
        # Песочница workflow запрещает os.path (воркер раскрывает ~ заранее;
        # ``~неизвестный`` остаётся как есть) — путь остаётся буквальным.
        return raw


def protection_enabled(settings: Any) -> bool:
    """Включена ли floor-защита системных путей:
    ``security.protected_paths.enabled`` (по умолчанию да).

    ``false`` снимает пол защищённых путей — вопрос и запрет защищённых
    путей, отказ служебных каталогов, отсечение команд вроде ``rm -rf .gc``.
    Запреты и вопросы режимов разрешений остаются: это другой слой.
    """
    return bool(settings_of(settings).security.protected_paths.enabled)
