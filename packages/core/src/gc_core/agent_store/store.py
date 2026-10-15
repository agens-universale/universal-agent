"""Хранилище агента — контракт для авторов хранилищ.

``ThreadStore`` держит то, что относится к одному треду. ``AgentStore`` — то,
что общее у всех тредов агента:

* промпты — по логическим именам :class:`~gc_core.agent_store.models.Prompt`;
* инструкции AGENTS.md — слои ``L0`` (роль запуска) и ``L1`` (глобальные);
* определения кастомных субагентов — ``subagent``, имена ``<файл>.md``;
* скиллы — ``skill``, имена ``<слой>/<путь>``, слои ``system``, ``user``,
  ``project``;
* память проекта — текст с версией, запись с проверкой версии.

Методы асинхронные и без приставки ``a``: контракт — публичная поверхность
для чужих авторов (исключение из правила ``docs/async.md``). Обработка текста
(декодирование, проверка на пустоту, подстановки, тексты отказов) — в ядре и
одинакова для любого хранилища: хранилище только отдаёт байты.

Чтение ресурса:

* ``None`` — ресурса у хранилища нет (например, путь к нему не настроен);
* ресурс есть, но не читается — ``OSError`` (в том числе
  ``FileNotFoundError`` у файла, на который показывает настройка).

Всё, что агент пишет о себе, — только через хранилища (``ThreadStore`` и
этот контракт): запись мимо них держит гейт
``tests/test_agent_data_writes_go_through_stores.py``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from gc_core.agent_store.models import (
    ProjectKey,
    ProjectMemory,
    ResourceInfo,
    ResourceKind,
)

if TYPE_CHECKING:
    from gc_core.tools.skills.registry import SkillsRegistry


class AgentStore(ABC):
    """Ресурсы и память агента."""

    @abstractmethod
    async def read(self, kind: ResourceKind, name: str) -> bytes | None:
        """Байты ресурса; ``None`` — ресурса нет."""

    @abstractmethod
    async def list(
        self, kind: ResourceKind, prefix: str = ""
    ) -> list[ResourceInfo]:
        """Содержимое каталога ресурсов ``prefix`` (субагенты, скиллы):
        имена внутри каталога по алфавиту. Нет каталога —
        ``FileNotFoundError`` или ``NotADirectoryError``."""

    @abstractmethod
    async def read_memory(self, project: ProjectKey) -> ProjectMemory | None:
        """Память проекта; ``None`` — её нет. Не читается — ``OSError``
        или ``UnicodeDecodeError``."""

    @abstractmethod
    async def write_memory(
        self,
        project: ProjectKey,
        text: str,
        *,
        expected_version: str | None,
    ) -> ProjectMemory:
        """Записать память целиком, если на месте версия ``expected_version``
        (``None`` — памяти нет или она пуста); иначе ``MemoryConflict``."""

    async def restore_memory(self, project: ProjectKey) -> None:
        """Вернуть память проекта из резервной копии, если её удалили
        (зовёт только тул ``project_memory``: статус и отпечаток памяти
        удалённое пользователем не возвращают). Без копий — ничего."""
        del project

    def label(self, kind: ResourceKind, name: str) -> str:
        """Как назвать ресурс в тексте отказа (файловое — путь)."""
        return f"{kind}:{name}"

    def memory_label(self, project: ProjectKey) -> str:
        """Как назвать память проекта в тексте отказа (файловое — путь)."""
        return f"project memory {project.id}"

    def skills_registry(
        self, settings: Any, *, backend: Any
    ) -> SkillsRegistry:
        """Реестр скиллов по дереву ``skill`` этого хранилища."""
        from gc_core.tools.skills.store_files import store_skills_registry

        return store_skills_registry(self, settings, runtime_backend=backend)

    async def aprovision_skills(
        self, backend: Any, settings: Any, *, run_logger: Any = None
    ) -> SkillsRegistry | None:
        """Реестр для среды исполнения; ``None`` — реестр из
        ``skills_registry`` годится как есть.

        Зовётся после сверки корня рана с бэкендом (``settings`` — уже
        сверенные): ``skills_registry`` строился раньше, на корне хоста, а
        скрипты своего хранилища выкладываются от корня среды исполнения.
        """
        return self.skills_registry(settings, backend=backend)
