"""Файловое хранилище агента — поведение ядра до хранилища v2.

Пути ресурсов хранилище вычисляет само по настройкам
(:mod:`gc_core.agent_store.file_paths`); память проекта — файл
``<root>/.gc/project_memory.md`` с атомарной записью и бэкапом вне дерева
проекта. Между процессами запись памяти не блокируется: проверка версии
защищает от потери правки внутри процесса и от устаревшего чтения, а для
нескольких инстансов на одном проекте — хранилище с базой.
"""

from __future__ import annotations

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


def memory_backup_path(project: ProjectKey) -> Path:
    """Бэкап памяти вне дерева проекта: переживает ``rm -rf .gc``."""
    from gc_core.config_modules.env_vars import gc_paths

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

    def _paths(self, kind: ResourceKind, name: str) -> list[str]:
        return resource_paths(self._settings, kind, name)

    def label(self, kind: ResourceKind, name: str) -> str:
        paths = self._paths(kind, name)
        return paths[0] if paths else f"{kind}:{name}"

    def memory_label(self, project: ProjectKey) -> str:
        return str(memory_path(project))

    async def read(self, kind: ResourceKind, name: str) -> bytes | None:
        paths = self._paths(kind, name)
        if not paths:
            return None
        *fallbacks, last = paths
        for path in fallbacks:
            data = _candidate(path, kind, name)
            if data is not None:
                return data
        return Path(last).read_bytes()

    async def list(
        self, kind: ResourceKind, prefix: str = ""
    ) -> list[ResourceInfo]:
        paths = self._paths(kind, prefix)
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
        from gc_core.project_memory import storage

        with storage._lock:
            storage.restore_from_backup(
                memory_path(project), memory_backup_path(project)
            )

    async def write_memory(
        self,
        project: ProjectKey,
        text: str,
        *,
        expected_version: str | None,
    ) -> ProjectMemory:
        from gc_core.project_memory import storage

        path = memory_path(project)
        with storage._lock:
            # Нечитаемую память (чужая кодировка) полная запись заменяет — как
            # и раньше: её версии нет.
            try:
                current = _memory_on_disk(path)
            except (OSError, UnicodeDecodeError):
                current = None
            version = current.version if current is not None else None
            if version != expected_version:
                raise MemoryConflict(current)
            storage._atomic_write(path, text)
            storage.mirror_backup(memory_backup_path(project), text)
        # Версия — та, что вернёт следующее чтение: файл читается с
        # приведением переводов строк (\r\n и \r → \n), иначе следующая
        # запись по этой версии упрётся в ложный конфликт.
        stored = text.replace("\r\n", "\n").replace("\r", "\n")
        return ProjectMemory(stored, memory_version(stored))

    # --- скиллы ---------------------------------------------------------------

    def skills_registry(
        self, settings: Any, *, backend: Any
    ) -> SkillsRegistry:
        from gc_core.tools.skills.registry import get_skills_registry

        return get_skills_registry(settings, backend=backend)

    async def aprovision_skills(
        self, backend: Any, settings: Any, *, run_logger: Any = None
    ) -> SkillsRegistry | None:
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


def _memory_on_disk(path: Path) -> ProjectMemory | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    return ProjectMemory(text, memory_version(text))
