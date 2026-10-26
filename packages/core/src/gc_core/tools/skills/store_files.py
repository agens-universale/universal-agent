"""Скиллы из своего хранилища агента: реестр поверх дерева ``skills``.

Реестр (``SkillsRegistry``) читает файлы через узкий набор операций бэкенда:
``listdir``, ``is_dir``, ``is_file``, ``iter_files``, ``read``, ``join``. Здесь
они отвечают по ресурсам ``skills`` хранилища агента: корни слоёв — ``system``,
``user``, ``project``, пути — ``<слой>/<скилл>/…`` с разделителем ``/``.

Скрипты скиллов отсюда не запускаются: скрипту нужен файл в среде исполнения,
а хранилище отдаёт только текст. ``resolve_script`` отвечает понятным отказом.
"""

from __future__ import annotations

import posixpath
from typing import Any, AsyncIterator, Dict

from gc_core.agent_store import AgentResource, AgentStore, DirEntry
from gc_core.tools.skills.registry import (
    SkillsRegistry,
    disabled_skills_from_config,
)

LAYERS = ("system", "user", "project")


class StoreSkillFiles:
    """Дерево ``skills`` хранилища агента как бэкенд для реестра скиллов."""

    def __init__(self, store: AgentStore) -> None:
        self._store = store

    def _resource(self, path: str) -> AgentResource:
        return AgentResource("skills", path.strip("/"))

    def _entries(self, path: str) -> list[DirEntry] | None:
        try:
            return self._store.list_dir(self._resource(path))
        except (FileNotFoundError, NotADirectoryError):
            return None

    @staticmethod
    def join(*parts: str) -> str:
        return posixpath.join(*parts)

    async def is_dir(self, path: str) -> bool:
        return self._entries(path) is not None

    async def is_file(self, path: str) -> bool:
        parent, name = posixpath.split(path.strip("/"))
        return (name, "file") in (self._entries(parent) or [])

    async def listdir(self, path: str) -> list:
        return [
            (name, kind == "dir") for name, kind in self._entries(path) or []
        ]

    async def iter_files(
        self, path: str, **_kwargs: Any
    ) -> AsyncIterator[str]:
        stack = [""]
        while stack:
            rel = stack.pop(0)
            for name, kind in self._entries(posixpath.join(path, rel)) or []:
                child = posixpath.join(rel, name) if rel else name
                if kind == "dir":
                    stack.append(child)
                else:
                    yield child

    async def read(self, path: str) -> str:
        return self._store.read_text(self._resource(path))


class StoreSkillsRegistry(SkillsRegistry):
    """Реестр скиллов своего хранилища агента: всё, кроме запуска скриптов."""

    async def resolve_script(
        self, skill_id: str, script_name: str
    ) -> Dict[str, Any]:
        skill = await self._get_skill(skill_id)
        raise FileNotFoundError(
            f"Скрипты скилла '{skill.id}' лежат в хранилище агента и в среде "
            "исполнения не запускаются: используйте инструкции, references и "
            "assets скилла."
        )


def store_skills_registry(store: AgentStore, settings: Any) -> SkillsRegistry:
    return StoreSkillsRegistry(
        backend=StoreSkillFiles(store),
        system_root="system",
        user_root="user",
        project_root="project",
        disable_skills=disabled_skills_from_config(settings),
    )


__all__ = [
    "LAYERS",
    "StoreSkillFiles",
    "StoreSkillsRegistry",
    "store_skills_registry",
]
