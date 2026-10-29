"""``gc-stores sync``: ресурсы агента — в хранилище агента в базе.

Контракт ядра ресурсы только читает; кладёт их сюда администратор: из
каталога в привычной раскладке и умолчания пакета gc-core
(``--with-defaults``, метка ``bundled@<версия>``). Неизменённое (по sha256)
не перезаписывается; ресурс, загруженный человеком, умолчание пакета не
перетирает.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from gc_core.agent_store import FileAgentStore, Prompt, ResourceKind
from gc_core.agent_store.roles import DEFAULT_ROLES, ROLE_PATTERN, role_variant
from gc_core.bundled_assets import bundled_root

from gc_stores.admin import ResourceWriter, StoredResource
from gc_stores.defaults import bundled_source


@dataclass
class SyncReport:
    """Что сделано: ``kind:name`` (с проектом — ``kind:project:name``)."""

    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)


def _label(kind: str, name: str, project: str | None) -> str:
    return f"{kind}:{project}:{name}" if project else f"{kind}:{name}"


async def _bundled() -> AsyncIterator[tuple[ResourceKind, str, bytes]]:
    """Промпты, роли поверхностей сервера и скиллы слоя ``system`` из
    пакета gc-core."""
    source = FileAgentStore()
    for name in Prompt:
        try:
            data = await source.read("prompt", name)
        except OSError:
            data = None
        if data is not None:
            yield "prompt", name.value, data
    prompts = bundled_root() / "prompts"
    for (kind, name), file in DEFAULT_ROLES.items():
        yield kind, name, (prompts / file).read_bytes()
    pending = ["system"]
    while pending:
        prefix = pending.pop()
        for entry in await source.list("skill", prefix):
            path = f"{prefix}/{entry.name}"
            if entry.type == "dir":
                pending.append(path)
                continue
            data = await source.read("skill", path)
            if data is not None:
                yield "skill", path, data


async def _put(
    writer: ResourceWriter,
    existing: dict[tuple[str, str, str | None], StoredResource],
    report: SyncReport,
    item: tuple[ResourceKind, str, bytes, str | None, str | None],
    *,
    dry_run: bool,
) -> None:
    kind, name, data, project, source = item
    label = _label(kind, name, project)
    current = existing.get((kind, name, project))
    if source is not None and current is not None and current.source is None:
        return  # умолчание пакета не перетирает ресурс человека
    digest = hashlib.sha256(data).hexdigest()
    if (
        current is not None
        and current.version == digest
        and current.source == source
    ):
        report.unchanged.append(label)
        return
    if not dry_run:
        written = await writer.put_resource(
            kind,
            name,
            data,
            project=project,
            source=source,
            preserve_manual=source is not None,
        )
        if written is False:
            return
    report.written.append(label)


async def _existing(
    writer: ResourceWriter, project: str | None
) -> dict[tuple[str, str, str | None], StoredResource]:
    return {
        (r.kind, r.name, r.project): r
        for r in await writer.list_resources(project=project)
    }


async def copy_defaults(
    writer: ResourceWriter, *, dry_run: bool = False
) -> SyncReport:
    """Промпты и системные скиллы пакета — в хранилище с меткой
    ``bundled@<версия>``; ресурс человека остаётся."""
    report = SyncReport()
    existing = await _existing(writer, None)
    source = bundled_source()
    async for kind, name, data in _bundled():
        await _put(
            writer,
            existing,
            report,
            (kind, name, data, None, source),
            dry_run=dry_run,
        )
    return report


#: Слои AGENTS.md и их область: L2, L3 — ресурсы проекта.
_INSTRUCTIONS = {"L0": False, "L1": False, "L2": True, "L3": True}
_SKILL_LAYERS = {"system": False, "user": False, "project": True}
_PROMPTS = frozenset(p.value for p in Prompt)
_SECTIONS = ("prompts", "instructions", "subagents", "skills", "roles")
#: Файлы роли: ``roles/<роль>/<файл>`` → ресурс, чей вариант роли он задаёт.
_ROLE_FILES: dict[str, tuple[ResourceKind, str]] = {
    "L0.md": ("instructions", "L0"),
    "special_instructions.md": ("prompt", Prompt.SPECIAL_INSTRUCTIONS.value),
}

Item = tuple[ResourceKind, str, bytes, str | None]


def _project_only(what: str, project: str | None) -> str:
    if project is None:
        raise ValueError(f"{what} — ресурс проекта: укажите --project")
    return project


def _single(rel: tuple[str, ...], section: str) -> str:
    if len(rel) != 2 or not rel[1].endswith(".md"):
        raise ValueError(
            f"{'/'.join(rel)}: в {section}/ — файлы <имя>.md без подкаталогов"
        )
    return rel[1][: -len(".md")]


def _prompt(rel: tuple[str, ...], data: bytes, _project: str | None) -> Item:
    name = _single(rel, "prompts")
    if name not in _PROMPTS:
        raise ValueError(
            f"{'/'.join(rel)}: неизвестный промпт {name!r}; допустимы: "
            + ", ".join(sorted(_PROMPTS))
        )
    return "prompt", name, data, None


def _instructions(
    rel: tuple[str, ...], data: bytes, project: str | None
) -> Item:
    name = _single(rel, "instructions")
    if name not in _INSTRUCTIONS:
        raise ValueError(
            f"{'/'.join(rel)}: слой инструкций — L0, L1, L2 или L3"
        )
    owner = _project_only(name, project) if _INSTRUCTIONS[name] else None
    return "instructions", name, data, owner


def _subagent(rel: tuple[str, ...], data: bytes, _project: str | None) -> Item:
    return "subagent", f"{_single(rel, 'subagents')}.md", data, None


def _skill(rel: tuple[str, ...], data: bytes, project: str | None) -> Item:
    layer = rel[1] if len(rel) > 2 else ""
    if layer not in _SKILL_LAYERS:
        raise ValueError(
            f"{'/'.join(rel)}: слой скиллов — system, user или project"
        )
    owner = (
        _project_only("слой скиллов project", project)
        if _SKILL_LAYERS[layer]
        else None
    )
    return "skill", "/".join(rel[1:]), data, owner


def _role(rel: tuple[str, ...], data: bytes, _project: str | None) -> Item:
    if (
        len(rel) != 3
        or not re.fullmatch(ROLE_PATTERN, rel[1])
        or rel[2] not in _ROLE_FILES
    ):
        raise ValueError(
            f"{'/'.join(rel)}: в roles/ — <роль>/L0.md и "
            "<роль>/special_instructions.md; роль — [A-Za-z0-9._-]"
        )
    kind, name = _ROLE_FILES[rel[2]]
    return kind, role_variant(name, rel[1]), data, None


_PARSERS = {
    "prompts": _prompt,
    "instructions": _instructions,
    "subagents": _subagent,
    "skills": _skill,
    "roles": _role,
}


def _files(root: Path) -> Iterator[tuple[str, ...]]:
    """Файлы каталога по порядку, без перехода по ссылкам: ссылка на файл
    или каталог вне каталога агента (ключи, секреты) ушла бы в хранилище,
    а оно бывает общим, — отказ. Скрытые (``.DS_Store``, ``.git``) — мимо."""
    for top, dirs, names in os.walk(root, followlinks=False):
        base = Path(top).relative_to(root).parts
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in [*dirs, *names]:
            if (Path(top) / name).is_symlink():
                raise ValueError(
                    f"{'/'.join((*base, name))}: символическая ссылка — sync "
                    "не идёт по ссылкам, положите файл в каталог агента"
                )
        for name in sorted(n for n in names if not n.startswith(".")):
            yield (*base, name)


def _layout(root: Path, project: str | None) -> Iterator[Item]:
    """Ресурсы каталога в раскладке ``gc-stores sync``; чужое —
    ``ValueError``."""
    for rel in _files(root):
        parser = _PARSERS.get(rel[0])
        if parser is None:
            raise ValueError(
                f"{'/'.join(rel)}: в каталоге агента — только "
                + ", ".join(f"{s}/" for s in _SECTIONS)
            )
        yield parser(rel, root.joinpath(*rel).read_bytes(), project)


def _merge(report: SyncReport, other: SyncReport) -> None:
    report.written += other.written
    report.unchanged += other.unchanged
    report.deleted += other.deleted


async def _prune(
    writer: ResourceWriter,
    existing: dict[tuple[str, str, str | None], StoredResource],
    present: set[tuple[str, str, str | None]],
    report: SyncReport,
    *,
    project: str | None,
    dry_run: bool,
) -> None:
    """Удалить загруженное человеком, чего нет в каталоге, — только в той
    же области (ресурсы агента или ресурсы проекта ``project``);
    умолчаний пакета не трогать."""
    for (kind, name, owner), stored in sorted(existing.items()):
        if (kind, name, owner) in present or stored.source is not None:
            continue
        if owner != project:
            continue
        if not dry_run:
            await writer.delete_resource(kind, name, project=owner)
        report.deleted.append(_label(kind, name, owner))


async def sync_directory(
    writer: ResourceWriter,
    root: Path,
    *,
    project: str | None = None,
    with_defaults: bool = False,
    prune: bool = False,
    dry_run: bool = False,
) -> SyncReport:
    """Разложить каталог агента ``root`` в хранилище.

    Раскладка: ``prompts/<имя Prompt>.md``, ``instructions/L0.md…L3.md``,
    ``subagents/<имя>.md``, ``skills/<слой>/<скилл>/…``,
    ``roles/<роль>/L0.md`` и ``roles/<роль>/special_instructions.md``
    (варианты роли поверхности: ``L0@<роль>``, ``special_instructions@<роль>``);
    ``L2``, ``L3`` и
    слой ``project`` — только с ``project``. ``with_defaults`` — сначала
    умолчания пакета (файлы каталога их перекрывают); ``prune`` — убрать
    своё, чего в каталоге нет; ``dry_run`` — только отчёт.
    """
    items = list(_layout(Path(root), project))
    report = SyncReport()
    if with_defaults:
        _merge(report, await copy_defaults(writer, dry_run=dry_run))
    existing = await _existing(writer, project)
    present = set()
    for kind, name, data, owner in items:
        present.add((kind, name, owner))
        await _put(
            writer,
            existing,
            report,
            (kind, name, data, owner, None),
            dry_run=dry_run,
        )
    if prune:
        await _prune(
            writer,
            existing,
            present,
            report,
            project=project,
            dry_run=dry_run,
        )
    return report


__all__ = ["SyncReport", "copy_defaults", "sync_directory"]
