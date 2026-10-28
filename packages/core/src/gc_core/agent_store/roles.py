"""Роль запуска в хранилище агента (``agent.role``).

У одного агента — несколько ролей (поверхности сервера): роль — свой
вариант L0 и специнструкций внутри агента, ресурсы ``L0@<роль>`` и
``special_instructions@<роль>``. Промпты, скиллы, субагенты и память
проекта — общие. Варианта роли нет — ресурса нет: общий L0 роль не
подменяет. Файловое хранилище роль не использует — у него роль задаёт
путь ``paths.agent_md_path``.
"""

from __future__ import annotations

from typing import Any

from gc_core.agent_store.file_store import FileAgentStore
from gc_core.agent_store.models import (
    ProjectKey,
    ProjectMemory,
    Prompt,
    ResourceInfo,
    ResourceKind,
)
from gc_core.agent_store.store import AgentStore
from gc_core.config_modules.agent import NAME_PATTERN

#: Имя роли — как ``agent.project_id``: оно попадает в ключи хранилища.
ROLE_PATTERN = NAME_PATTERN

#: Роли поверхностей сервера по умолчанию: ресурс роли → файл пакетных
#: промптов (те же тексты, что у сервера на файлах). Кладёт их
#: ``gc-stores sync --with-defaults``.
DEFAULT_ROLES: dict[tuple[ResourceKind, str], str] = {
    ("instructions", "L0@webhook"): "server_agent.md",
    (
        "prompt",
        "special_instructions@chat",
    ): "chat_completions_responses_special_instructions.md",
    (
        "prompt",
        "special_instructions@chat.completions",
    ): "chat_completions_completions_special_instructions.md",
}


def role_variant(name: str, role: str) -> str:
    """Имя ресурса роли: ``L0@webhook``, ``special_instructions@chat``."""
    return f"{name}@{role}"


class RoleAgentStore(AgentStore):
    """Хранилище агента глазами роли: L0 и специнструкции — варианты роли
    ``role`` (специнструкции — ``special_role``, по умолчанию та же роль),
    остальное — как у ``store``."""

    def __init__(
        self,
        store: AgentStore,
        *,
        role: str,
        special_role: str | None = None,
    ) -> None:
        self.store = store
        self.role = role
        self.special_role = special_role or role

    def _name(self, kind: ResourceKind, name: str) -> str:
        if kind == "instructions" and name == "L0":
            return role_variant(name, self.role)
        if kind == "prompt" and name == Prompt.SPECIAL_INSTRUCTIONS:
            return role_variant(name, self.special_role)
        return name

    async def read(
        self,
        kind: ResourceKind,
        name: str,
        *,
        project: ProjectKey | None = None,
    ) -> bytes | None:
        return await self.store.read(
            kind, self._name(kind, name), project=project
        )

    async def list(
        self,
        kind: ResourceKind,
        prefix: str = "",
        *,
        project: ProjectKey | None = None,
    ) -> list[ResourceInfo]:
        return await self.store.list(kind, prefix, project=project)

    async def read_memory(self, project: ProjectKey) -> ProjectMemory | None:
        return await self.store.read_memory(project)

    async def write_memory(
        self,
        project: ProjectKey,
        text: str,
        *,
        expected_version: str | None,
    ) -> ProjectMemory:
        return await self.store.write_memory(
            project, text, expected_version=expected_version
        )

    async def restore_memory(self, project: ProjectKey) -> None:
        await self.store.restore_memory(project)

    def label(
        self,
        kind: ResourceKind,
        name: str,
        *,
        project: ProjectKey | None = None,
    ) -> str:
        return self.store.label(kind, self._name(kind, name), project=project)

    def memory_label(self, project: ProjectKey) -> str:
        return self.store.memory_label(project)

    def skills_registry(
        self,
        settings: Any,
        *,
        backend: Any,
        project: ProjectKey | None = None,
    ) -> Any:
        return self.store.skills_registry(
            settings, backend=backend, project=project
        )

    async def aprovision_skills(
        self,
        backend: Any,
        settings: Any,
        *,
        run_logger: Any = None,
        project: ProjectKey | None = None,
    ) -> Any:
        return await self.store.aprovision_skills(
            backend, settings, run_logger=run_logger, project=project
        )

    def __getattr__(self, name: str) -> Any:
        # Возможности хранилища вне контракта (схема, закрытие) — как есть.
        return getattr(self.store, name)


def with_role(store: AgentStore, settings: Any) -> AgentStore:
    """Хранилище рана с ролью ``agent.role`` настроек ``settings``; без
    роли и у файлового хранилища — ``store`` как есть."""
    from gc_core.settings import settings_of

    agent = settings_of(settings).agent
    if not agent.role or isinstance(store, (FileAgentStore, RoleAgentStore)):
        return store
    return RoleAgentStore(
        store, role=agent.role, special_role=agent.special_instructions_role
    )


__all__ = [
    "DEFAULT_ROLES",
    "ROLE_PATTERN",
    "RoleAgentStore",
    "role_variant",
    "with_role",
]
