"""Файловое хранилище агента — поведение ядра до хранилища v2.

Пути ресурсов хранилище вычисляет само по настройкам
(:mod:`gc_core.agent_store.file_paths`); память проекта — файл
``<root>/.gc/project_memory.md`` с атомарной записью и бэкапом вне дерева
проекта. Между процессами запись памяти не блокируется: проверка версии
защищает от потери правки внутри процесса и от устаревшего чтения, а для
нескольких инстансов на одном проекте — хранилище с базой.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gc_core.agent_store.errors import MemoryConflict
from gc_core.agent_store.file_paths import resource_paths
from gc_core.agent_store.models import (
    ProjectKey,
    ProjectMemory,
    ResourceInfo,
    ResourceKind,
    memory_version,
    needs_project,
)
from gc_core.agent_store.store import AgentStore

if TYPE_CHECKING:
    from gc_core.tools.skills.registry import SkillsRegistry

_log = logging.getLogger(__name__)

_MEMORY_FILE = "project_memory.md"
_BACKUP_FILE = "GIGACODE_memory_backup.md"


def memory_path(project: ProjectKey) -> Path:
    if not project.root:
        raise ValueError(
            f"у проекта {project.id!r} нет каталога: файловому хранилищу "
            "негде держать память"
        )
    return Path(project.root) / ".gc" / _MEMORY_FILE


def _memory_lock(project: ProjectKey):
    """Замок памяти проекта между процессами (TUI и gc-cli в одном
    репозитории): сверка версии и запись — под ним. Ключ — файл памяти, а не
    ``project_id``: процессы с разным id на одном репозитории пишут один
    файл. Файл замка — в каталоге состояния, а не в ``.gc`` репозитория."""
    import hashlib

    from gc_core.config_modules.env_vars import gc_paths
    from gc_core.utils.file_lock import file_lock

    target = str(memory_path(project).resolve())
    digest = hashlib.sha256(target.encode("utf-8")).hexdigest()[:24]
    return file_lock(Path(gc_paths()["state"]) / "memory-locks" / digest)


def memory_backup_path(project: ProjectKey) -> Path:
    """Бэкап памяти вне дерева проекта: переживает ``rm -rf .gc``.

    id проекта — имя каталога: с разделителем или ``..`` он вывел бы бэкап за
    каталог состояния (id из SDK встраивающего не проходит проверку
    конфига) — ``ValueError``."""
    from gc_core.config_modules.env_vars import gc_paths

    if (
        not project.id
        or project.id in (".", "..")
        or any(sep in project.id for sep in ("/", "\\", "\0"))
    ):
        raise ValueError(f"id проекта {project.id!r} — не имя каталога")
    return gc_paths()["state"] / "projects" / project.id / _BACKUP_FILE


class FileAgentStore(AgentStore):
    """Файлы на диске процесса по путям из ``settings`` (с runtime-слоем
    фронтенда — его подставляют ACP, TUI и chat)."""

    def __init__(self, settings: Any = None) -> None:
        from gc_core.settings import settings_of

        self._settings = (
            _packaged_defaults() if settings is None else settings_of(settings)
        )

    # --- ресурсы --------------------------------------------------------------

    def _paths(
        self, kind: ResourceKind, name: str, project: ProjectKey | None
    ) -> list[str]:
        return resource_paths(self._settings, kind, name, project)

    def label(
        self,
        kind: ResourceKind,
        name: str,
        *,
        project: ProjectKey | None = None,
    ) -> str:
        paths = self._paths(kind, name, project)
        return paths[0] if paths else f"{kind}:{name}"

    def memory_label(self, project: ProjectKey) -> str:
        return str(memory_path(project))

    async def read(
        self,
        kind: ResourceKind,
        name: str,
        *,
        project: ProjectKey | None = None,
    ) -> bytes | None:
        paths = self._paths(kind, name, project)
        if not paths:
            return None
        *fallbacks, last = paths
        for path in fallbacks:
            data = _candidate(path, kind, name)
            if data is not None:
                return data
        if needs_project(kind, name) and not os.path.isfile(last):
            # Слой проекта — необязательный файл репозитория: нет — нет.
            return None
        return Path(last).read_bytes()

    async def list(
        self,
        kind: ResourceKind,
        prefix: str = "",
        *,
        project: ProjectKey | None = None,
    ) -> list[ResourceInfo]:
        paths = self._paths(kind, prefix, project)
        if not paths:
            raise FileNotFoundError(f"{kind}:{prefix}")
        directory = paths[0]
        if not os.path.isdir(directory):
            raise NotADirectoryError(directory)
        found = []
        for entry in sorted(os.listdir(directory)):
            full = os.path.join(directory, entry)
            if os.path.isdir(full):
                found.append(ResourceInfo(entry, "dir"))
            elif os.path.isfile(full):
                found.append(ResourceInfo(entry, "file"))
        return found

    # --- память проекта -------------------------------------------------------

    async def read_memory(self, project: ProjectKey) -> ProjectMemory | None:
        from gc_core.project_memory import storage

        with storage._lock:
            return _memory_on_disk(memory_path(project))

    async def restore_memory(self, project: ProjectKey) -> None:
        await asyncio.to_thread(_restore_memory_locked, project)

    async def write_memory(
        self,
        project: ProjectKey,
        text: str,
        *,
        expected_version: str | None,
    ) -> ProjectMemory:
        # Ожидание замка (до секунд, пока пишет другой процесс) — в потоке:
        # на loop стояли бы все поверхности сервера.
        await asyncio.to_thread(
            _write_memory_locked, project, text, expected_version
        )
        # Версия — та, что вернёт следующее чтение: файл читается с
        # приведением переводов строк (\r\n и \r → \n), иначе следующая
        # запись по этой версии упрётся в ложный конфликт.
        stored = text.replace("\r\n", "\n").replace("\r", "\n")
        return ProjectMemory(stored, memory_version(stored))

    # --- скиллы ---------------------------------------------------------------

    def skills_registry(
        self,
        settings: Any,
        *,
        backend: Any,
        project: ProjectKey | None = None,
    ) -> SkillsRegistry:
        # Слой project — скиллы проекта рана (<root>/.gc/skills): ход chat
        # или a2a идёт в каталоге клиента, а не в cwd процесса.
        from gc_core.tools.skills.registry import get_skills_registry

        root = project.root if project is not None else None
        return get_skills_registry(
            settings,
            backend=backend,
            project_root=str(Path(root) / ".gc" / "skills") if root else None,
        )

    async def aprovision_skills(
        self,
        backend: Any,
        settings: Any,
        *,
        run_logger: Any = None,
        project: ProjectKey | None = None,
    ) -> SkillsRegistry | None:
        del project
        # Remote-бэкенд: скиллы лежат на хосте, а реестр читает через бэкенд —
        # набор копируется «туда», реестр собирается по удалённым корням.
        from gc_core.tools.skills.provision import aprovision_remote_skills

        return await aprovision_remote_skills(
            backend, settings, run_logger=run_logger
        )


def _packaged_defaults() -> Any:
    """Настройки по умолчанию с путями пакетных ассетов (как у конфига без
    своих путей): промпты — из пакета."""
    from gc_core.bundled_assets import bundled_root
    from gc_core.config_modules.root import AppConfig, resolve_paths

    return resolve_paths(AppConfig(), str(bundled_root()))


def _candidate(path: str, kind: str, name: str) -> bytes | None:
    """Звено цепочки путей: нет, не читается или пуст — следующее звено."""
    try:
        data = Path(path).read_bytes()
        data.decode("utf-8")
    except FileNotFoundError:
        return None
    # Звено — путь ИЗ КОНФИГА: файл человека, кодировка его. Отказ — откат на
    # следующее звено (пакетный файл), а не падение запуска.
    except (OSError, UnicodeDecodeError):
        _log.warning(
            "Failed to read %s %s from %s", kind, name, path, exc_info=True
        )
        return None
    return data if data.strip() else None


def _write_memory_locked(
    project: ProjectKey, text: str, expected_version: str | None
) -> None:
    """Сверка версии и запись памяти — под замком файла памяти."""
    from gc_core.project_memory import storage

    path = memory_path(project)
    with storage._lock, _memory_lock(project):
        # Нечитаемую память (чужая кодировка) полная запись заменяет — как и
        # раньше: её версии нет.
        try:
            current = _memory_on_disk(path)
        except (OSError, UnicodeDecodeError):
            current = None
        version = current.version if current is not None else None
        if version != expected_version:
            raise MemoryConflict(current)
        storage._atomic_write(path, text)
        storage.mirror_backup(memory_backup_path(project), text)


def _restore_memory_locked(project: ProjectKey) -> None:
    from gc_core.project_memory import storage

    with storage._lock, _memory_lock(project):
        storage.restore_from_backup(
            memory_path(project), memory_backup_path(project)
        )


def _memory_on_disk(path: Path) -> ProjectMemory | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    return ProjectMemory(text, memory_version(text))
