"""``MemoryAgentStore`` — хранилище агента в памяти: вторая реализация контракта.

Как ``MemoryThreadStore`` для тредов, живёт в тест-ките: доказывает, что
контракт ``AgentStore`` годен для хранилища не на диске (база, S3), и служит
образцом своего хранилища.

Ресурсы — словари по видам: ``prompts`` и ``instructions`` — имя → текст,
``subagents`` — имя файла → текст определения, ``skills`` — путь
``<слой>/<скилл>/…`` → текст. Чего в словарях нет, читается у ``fallback``
(по умолчанию — файловое хранилище: пакетные промпты сжатия, finish gate…).
Ресурсы проекта (``L2``, ``L3``, скиллы слоя ``project``) — ``projects``:
``{id проекта: {"instructions": {...}, "skill": {...}}}``. Память проектов —
в словаре по ``ProjectKey.id``.
"""

from __future__ import annotations

from typing import Mapping

from gc_core.agent_store import (
    AgentStore,
    FileAgentStore,
    MemoryConflict,
    ProjectKey,
    ProjectMemory,
    ResourceInfo,
    memory_version,
    needs_project,
    require_project,
)

_DEFAULT = object()


class MemoryAgentStore(AgentStore):
    def __init__(
        self,
        *,
        prompts: Mapping[str, str] | None = None,
        instructions: Mapping[str, str] | None = None,
        subagents: Mapping[str, str] | None = None,
        skills: Mapping[str, str] | None = None,
        memory: Mapping[str, str] | None = None,
        projects: Mapping[str, Mapping[str, Mapping[str, str]]] | None = None,
        fallback: AgentStore | object | None = _DEFAULT,
    ) -> None:
        self._data = {
            "prompt": dict(prompts or {}),
            "instructions": dict(instructions or {}),
            "subagent": dict(subagents or {}),
            "skill": dict(skills or {}),
        }
        self._projects = {
            pid: {kind: dict(texts) for kind, texts in data.items()}
            for pid, data in (projects or {}).items()
        }
        self._memory: dict[str, str] = dict(memory or {})
        self._fallback = FileAgentStore() if fallback is _DEFAULT else fallback
        #: Что читали: ``(вид, имя)`` — тесты проверяют, что шло через хранилище.
        self.reads: list[tuple[str, str]] = []

    def _texts(self, kind, name: str, project) -> dict[str, str] | None:
        """Словарь ресурсов для имени; ``None`` — ресурс проекта без
        ресурсов этого проекта (фолбэк не спрашивается)."""
        require_project(kind, name, project)
        if needs_project(kind, name):
            return self._projects.get(project.id, {}).get(kind, {})
        return self._data[kind]

    def label(self, kind, name: str, *, project=None) -> str:
        if (
            needs_project(kind, name)
            or name in self._data[kind]
            or self._fallback is None
        ):
            return super().label(kind, name, project=project)
        return self._fallback.label(kind, name)

    async def read(self, kind, name: str, *, project=None) -> bytes | None:
        self.reads.append((kind, name))
        texts = self._texts(kind, name, project)
        if name in texts:
            return texts[name].encode("utf-8")
        if self._fallback is not None and not needs_project(kind, name):
            return await self._fallback.read(kind, name)
        return None

    async def list(
        self, kind, prefix: str = "", *, project=None
    ) -> list[ResourceInfo]:
        texts = self._texts(kind, prefix, project)
        if not texts:
            if self._fallback is not None and not needs_project(kind, prefix):
                return await self._fallback.list(kind, prefix)
            raise FileNotFoundError(f"{kind}:{prefix}")
        head = prefix.strip("/")
        head = f"{head}/" if head else ""
        entries: dict[str, str] = {}
        for path in texts:
            if not path.startswith(head):
                continue
            name, sep, _rest = path[len(head) :].partition("/")
            entries[name] = "dir" if sep else "file"
        if not entries:
            raise FileNotFoundError(f"{kind}:{prefix}")
        return [ResourceInfo(n, t) for n, t in sorted(entries.items())]

    async def read_memory(self, project: ProjectKey) -> ProjectMemory | None:
        text = self._memory.get(project.id)
        return (
            None if text is None else ProjectMemory(text, memory_version(text))
        )

    async def write_memory(
        self,
        project: ProjectKey,
        text: str,
        *,
        expected_version: str | None,
    ) -> ProjectMemory:
        current = await self.read_memory(project)
        version = current.version if current is not None else None
        if version != expected_version:
            raise MemoryConflict(current)
        self._memory[project.id] = text
        return ProjectMemory(text, memory_version(text))


__all__ = ["MemoryAgentStore"]
