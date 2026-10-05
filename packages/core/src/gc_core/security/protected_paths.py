from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gc_core.attachments.store import STAGING_DIRNAME
from gc_core.config_modules.env_vars import gc_paths, project_gc_dir
from gc_core.instructions.layers import instruction_file_paths
from gc_core.security.path_policy import is_within_root
from gc_core.settings import settings_of


@dataclass(frozen=True)
class ProtectedSet:
    instructions: frozenset[Path]
    operational: frozenset[Path]
    skills_exceptions: frozenset[Path]
    # Корни класса `media` (`security.media_roots`): чтение — да, запись тулами —
    # нет, команды — с подтверждением. Пусто — класса нет: так набор собирают
    # вызывающие без конфига (судья команд, diff-TUI).
    media: frozenset[Path] = frozenset()


def protected_paths(
    *,
    cwd: str | None = None,
    extra: list[str] | None = None,
    media: Iterable[str | os.PathLike[str]] = (),
) -> ProtectedSet:
    proj = Path(cwd) if cwd else Path(os.getcwd())
    paths = gc_paths()
    instr = set(instruction_file_paths(cwd=str(proj)).values())

    operational = {project_gc_dir(str(proj))}
    for key in ("config", "state", "cache", "data"):
        operational.add(paths[key])
    for raw in extra or []:
        operational.add(Path(os.path.abspath(raw)))

    skills = {project_gc_dir(str(proj)) / "skills", paths["data"] / "skills"}

    return ProtectedSet(
        instructions=frozenset(instr),
        operational=frozenset(operational),
        skills_exceptions=frozenset(skills),
        media=frozenset(
            Path(os.path.abspath(os.fspath(root))) for root in media
        ),
    )


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
    ci = os.name == "nt"
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
    if any(is_within_root(p, r, case_insensitive=ci) for r in ps.operational):
        return "operational"
    return "none"


def protected_decision(path, *, cwd=None, extra=None, media=()) -> str:
    cls = classify_path(
        path, protected_paths(cwd=cwd, extra=extra, media=media)
    )
    if cls == "instructions":
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
    return [
        os.path.expanduser(raw)
        for raw in settings_of(source).security.protected_paths.extra
        if str(raw).strip()
    ]


def protection_enabled(settings: Any) -> bool:
    """Включена ли floor-защита системных путей — всегда да.

    ``security.protected_paths.enabled`` сейчас не действует (см.
    docs/configuration.md): до гардов этот флаг не доходил никогда — плоский
    вид настроек секцию не проецировал, — и значение ``false`` защиту не
    снимает. ``settings`` (то, что принимает ``settings_of``) оставлен ради
    сигнатуры: включать флаг — отдельное решение, а не побочный эффект
    перехода на типизированные настройки.
    """
    del settings
    return True
