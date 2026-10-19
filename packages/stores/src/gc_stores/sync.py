"""``gc-stores sync``: ресурсы агента — в хранилище агента в базе.

Контракт ядра ресурсы только читает; кладёт их сюда администратор: из
каталога в привычной раскладке и умолчания пакета gc-core
(``--with-defaults``, метка ``bundled@<версия>``). Неизменённое (по sha256)
не перезаписывается; ресурс, загруженный человеком, умолчание пакета не
перетирает.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from gc_core.agent_store import FileAgentStore, Prompt, ResourceKind

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
    """Промпты и скиллы слоя ``system`` из пакета gc-core."""
    source = FileAgentStore()
    for name in Prompt:
        try:
            data = await source.read("prompt", name)
        except OSError:
            data = None
        if data is not None:
            yield "prompt", name.value, data
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
        await writer.put_resource(
            kind, name, data, project=project, source=source
        )
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


__all__ = ["SyncReport", "copy_defaults"]
