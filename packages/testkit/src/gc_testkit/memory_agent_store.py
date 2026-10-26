"""``MemoryAgentStore`` — хранилище агента в памяти: вторая реализация контракта.

Как ``MemoryThreadStore`` для тредов, живёт в тест-ките: доказывает, что
контракт ``AgentStore`` годен для хранилища не на диске (база, S3), и служит
образцом своего хранилища.

Ресурсы — словари по видам: ``prompts`` и ``instructions`` — имя → текст,
``subagents`` — имя файла → текст определения, ``skills`` — путь
``<слой>/<скилл>/…`` → текст. Чего в словарях нет, читается у ``fallback``
(по умолчанию — файловое хранилище: пакетные промпты сжатия, finish gate…).
"""

from __future__ import annotations

from typing import Mapping

from gc_core.agent_store import (
    DEFAULT_AGENT_STORE,
    AgentResource,
    AgentStore,
    DirEntry,
)


class MemoryAgentStore(AgentStore):
    def __init__(
        self,
        *,
        prompts: Mapping[str, str] | None = None,
        instructions: Mapping[str, str] | None = None,
        subagents: Mapping[str, str] | None = None,
        skills: Mapping[str, str] | None = None,
        fallback: AgentStore | None = DEFAULT_AGENT_STORE,
    ) -> None:
        self._data = {
            "prompt": dict(prompts or {}),
            "instructions": dict(instructions or {}),
            "subagents": dict(subagents or {}),
            "skills": dict(skills or {}),
        }
        self._fallback = fallback
        #: Что читали: ``(вид, имя)`` — тесты проверяют, что шло через хранилище.
        self.reads: list[tuple[str, str]] = []

    def read_text(self, resource: AgentResource) -> str:
        self.reads.append((resource.kind, resource.name))
        texts = self._data[resource.kind]
        if resource.name in texts:
            return texts[resource.name]
        if self._fallback is not None:
            return self._fallback.read_text(resource)
        raise FileNotFoundError(resource.label)

    def list_dir(self, resource: AgentResource) -> list[DirEntry]:
        texts = self._data[resource.kind]
        if not texts:
            if self._fallback is not None:
                return self._fallback.list_dir(resource)
            raise FileNotFoundError(resource.label)
        prefix = resource.name.strip("/")
        prefix = f"{prefix}/" if prefix else ""
        entries: dict[str, str] = {}
        for path in texts:
            if not path.startswith(prefix):
                continue
            head, sep, _rest = path[len(prefix) :].partition("/")
            entries[head] = "dir" if sep else "file"
        if not entries:
            raise FileNotFoundError(resource.label)
        return sorted(entries.items())


__all__ = ["MemoryAgentStore"]
