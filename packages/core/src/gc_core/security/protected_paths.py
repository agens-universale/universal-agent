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
    if any(
        is_within_root(p, r, case_insensitive=ci) for r in ps.operational_hard
    ):
        return "operational"
    if ps.workspace_exception is not None and is_within_root(
        p, ps.workspace_exception, case_insensitive=ci
    ):
        return "none"
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
