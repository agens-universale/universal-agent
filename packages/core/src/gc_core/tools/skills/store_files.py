"""Скиллы из своего хранилища агента: реестр поверх дерева ``skills``.

Реестр (``SkillsRegistry``) читает файлы через узкий набор операций бэкенда:
``listdir``, ``is_dir``, ``is_file``, ``iter_files``, ``read``, ``join``. Здесь
они отвечают по ресурсам ``skills`` хранилища агента: корни слоёв — ``system``,
``user``, ``project``, пути — ``<слой>/<скилл>/…`` с разделителем ``/``.

Скрипту нужен файл в среде исполнения, а хранилище отдаёт байты: перед
запуском файлы скилла выкладываются через ``os_backend`` в
``<launch_root>/.gc/skills_runtime/<слой>/<скилл>`` с манифестом
``.manifest.sha256`` (повторный запуск с тем же набором ничего не копирует) —
как выкладка скиллов в docker/ssh (``provision.py``).
"""

from __future__ import annotations

import posixpath
from typing import Any, AsyncIterator, Dict

from gc_core.agent_store import AgentStore, ResourceInfo, decode_text
from gc_core.tools.skills.registry import (
    SkillsRegistry,
    disabled_skills_from_config,
)

LAYERS = ("system", "user", "project")


class StoreSkillFiles:
    """Дерево ``skills`` хранилища агента как бэкенд для реестра скиллов."""

    def __init__(self, store: AgentStore) -> None:
        self._store = store

    async def _entries(self, path: str) -> list[ResourceInfo] | None:
        try:
            return await self._store.list("skill", path.strip("/"))
        except (FileNotFoundError, NotADirectoryError):
            return None

    @staticmethod
    def join(*parts: str) -> str:
        return posixpath.join(*parts)

    async def is_dir(self, path: str) -> bool:
        return await self._entries(path) is not None

    async def is_file(self, path: str) -> bool:
        parent, name = posixpath.split(path.strip("/"))
        return any(
            entry.name == name and entry.type == "file"
            for entry in await self._entries(parent) or []
        )

    async def listdir(self, path: str) -> list:
        return [
            (entry.name, entry.type == "dir")
            for entry in await self._entries(path) or []
        ]

    async def iter_files(
        self, path: str, **_kwargs: Any
    ) -> AsyncIterator[str]:
        stack = [""]
        while stack:
            rel = stack.pop(0)
            entries = await self._entries(posixpath.join(path, rel))
            for entry in entries or []:
                child = posixpath.join(rel, entry.name) if rel else entry.name
                if entry.type == "dir":
                    stack.append(child)
                else:
                    yield child

    async def read(self, path: str) -> str:
        """Текст файла; нет — ``FileNotFoundError``, как у файлового бэкенда."""
        name = path.strip("/")
        data = await self._store.read("skill", name)
        if data is None:
            raise FileNotFoundError(self._store.label("skill", name))
        return decode_text(data)


class StoreSkillsRegistry(SkillsRegistry):
    """Реестр скиллов своего хранилища агента; скрипты — после выкладки
    скилла в среду исполнения."""

    def __init__(
        self,
        *,
        store: AgentStore,
        settings: Any = None,
        runtime_backend: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(backend=StoreSkillFiles(store), **kwargs)
        self._store = store
        self._settings = settings
        self._runtime = runtime_backend

    async def resolve_script(
        self, skill_id: str, script_name: str
    ) -> Dict[str, Any]:
        skill = await self._get_skill(skill_id)
        normalized = (script_name or "").strip().replace("\\", "/")
        if normalized.startswith("scripts/"):
            normalized = normalized[len("scripts/") :]
        if not normalized or normalized not in skill.scripts:
            raise FileNotFoundError(
                f"Script '{script_name}' not found for skill '{skill_id}'. "
                f"Available scripts: {skill.scripts}"
            )
        root = await self._aprovision(skill)
        return {
            "path": posixpath.join(root, "scripts", normalized),
            "script_name": normalized,
            "available_scripts": skill.scripts,
        }

    async def _aprovision(self, skill: Any) -> str:
        """Выложить файлы скилла в среду исполнения; вернуть его корень там."""
        from gc_core.tools.skills import provision

        launch_root = (
            provision._remote_root(self._settings, self._runtime)
            if self._runtime is not None
            else None
        )
        if not launch_root:
            raise FileNotFoundError(
                f"Скрипты скилла '{skill.id}' лежат в хранилище агента, а "
                "среды исполнения, куда их выложить, у этого реестра нет: "
                "используйте инструкции, references и assets скилла."
            )
        files = [
            (rel, await self._abytes(posixpath.join(skill.root_path, rel)))
            async for rel in self._backend.iter_files(skill.root_path)
        ]
        files.sort(key=lambda item: item[0])
        digest = provision._digest(files)
        target = posixpath.join(
            launch_root, provision.RUNTIME_SUBDIR, skill.root_path
        )
        manifest = posixpath.join(target, provision.MANIFEST_NAME)
        if await provision._remote_manifest(self._runtime, manifest) == digest:
            return target
        for rel, data in files:
            path = posixpath.join(target, rel)
            await self._runtime.makedirs(posixpath.dirname(path))
            await self._runtime.write_bytes(path, data)
        # Манифест — последним: незаконченная выкладка повторится.
        await self._runtime.makedirs(target)
        await self._runtime.write(manifest, digest + "\n")
        return target

    async def _abytes(self, name: str) -> bytes:
        data = await self._store.read("skill", name.strip("/"))
        if data is None:
            raise FileNotFoundError(self._store.label("skill", name))
        return data


def store_skills_registry(
    store: AgentStore, settings: Any, *, runtime_backend: Any = None
) -> SkillsRegistry:
    return StoreSkillsRegistry(
        store=store,
        settings=settings,
        runtime_backend=runtime_backend,
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
