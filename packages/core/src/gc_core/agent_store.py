"""Хранилище агента: ресурсы, одинаковые для всех тредов агента.

``ThreadStore`` держит то, что относится к одному треду (история, meta,
картинки). ``AgentStore`` — то, что общее у всех тредов агента и что ядро
только читает:

* промпты — системный, ``special_instructions``, субагентов, служебные
  (сжатие, finish gate, intent, судья команд…);
* инструкции AGENTS.md — слои ``L0``–``L3``;
* определения кастомных субагентов;
* скиллы.

Ресурс — :class:`AgentResource`: вид, логическое имя и путь, который ядро
вычислило как раньше (из конфига или пакета). :class:`FileAgentStore` — по
умолчанию, если в SDK ничего не передали: читает файл по пути, то есть ровно то
и оттуда, что ядро читало до хранилища. Своё хранилище (база, S3, конфиг-сервис)
отвечает по виду и имени, путь для него — подсказка. Что своё хранилище не
переопределило, удобно брать как раньше — наследуйтесь от ``FileAgentStore`` и
зовите ``super()``.

Логические имена::

    prompt        system, special_instructions, subagent, custom_subagent,
                  summary, memory_patch, limit_summary, intent_tool,
                  intent_final, finish_gate, project_memory_compaction,
                  command_judge_system, command_judge_user,
                  action_judge_system, action_judge_user
    instructions  L0 (роль запуска), L1 (глобальные), L2 (.gc/AGENTS.md),
                  L3 (AGENTS.md проекта)
    subagents     ""  — каталог определений; "<файл>.md" — определение
    skills        "<слой>/<путь>" — дерево скиллов, слои system / user / project

Обработка текста (проверка на пустоту, подстановки, тексты отказов) остаётся в
ядре и одинакова для любого хранилища. Хранилище только отдаёт текст.

Скрипты скиллов из своего хранилища не запускаются: текст скилла, references и
assets читаются через хранилище, а скрипту нужен файл в среде исполнения.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from gc_core.utils.user_text import unreadable_reason

if TYPE_CHECKING:
    from gc_core.tools.skills.registry import SkillsRegistry

AgentResourceKind = Literal["prompt", "instructions", "subagents", "skills"]
#: Запись каталога: имя и ``"file"`` / ``"dir"``.
DirEntry = tuple[str, Literal["file", "dir"]]


@dataclass(frozen=True)
class AgentResource:
    """Что читать: вид, логическое имя и путь файлового хранилища.

    ``path is None`` — путь в конфиге не задан: файловое хранилище такого
    ресурса не имеет, своё может отдать его по имени.
    """

    kind: AgentResourceKind
    name: str
    path: str | None = None

    @property
    def label(self) -> str:
        """Чем назвать ресурс в тексте отказа: путь или ``вид:имя``."""
        return self.path or f"{self.kind}:{self.name}"


class AgentStore(ABC):
    """Ресурсы агента, только чтение. Синхронный контракт, как у ``ThreadStore``."""

    @abstractmethod
    def read_text(self, resource: AgentResource) -> str:
        """Текст ресурса.

        Нет ресурса — ``FileNotFoundError``; есть, но не читается — ``OSError``
        или ``UnicodeDecodeError``: так же, как ``Path.read_text``, поэтому
        ядро отвечает на отказ одинаково для любого хранилища.
        """

    @abstractmethod
    def list_dir(self, resource: AgentResource) -> list[DirEntry]:
        """Содержимое каталога ресурса (субагенты, скиллы).

        Нет каталога — ``FileNotFoundError`` или ``NotADirectoryError``.
        """

    def skills_registry(
        self, settings: Any, *, backend: Any
    ) -> SkillsRegistry:
        """Реестр скиллов по дереву ``skills`` этого хранилища."""
        from gc_core.tools.skills.store_files import store_skills_registry

        return store_skills_registry(self, settings)

    async def aprovision_skills(
        self, backend: Any, settings: Any, *, run_logger: Any = None
    ) -> SkillsRegistry | None:
        """Реестр для среды исполнения, если скиллы надо туда доставить.

        ``None`` — реестр из ``skills_registry`` годится как есть.
        """
        return None


class FileAgentStore(AgentStore):
    """Файлы на диске процесса — поведение ядра до хранилища агента.

    Состояния нет: пути вычисляет ядро по текущим настройкам на каждом чтении.
    """

    def read_text(self, resource: AgentResource) -> str:
        if resource.path is None:
            raise FileNotFoundError(resource.label)
        return Path(resource.path).read_text(encoding="utf-8")

    def list_dir(self, resource: AgentResource) -> list[DirEntry]:
        if resource.path is None:
            raise FileNotFoundError(resource.label)
        if not os.path.isdir(resource.path):
            raise NotADirectoryError(resource.path)
        entries: list[DirEntry] = []
        for name in sorted(os.listdir(resource.path)):
            full = os.path.join(resource.path, name)
            if os.path.isdir(full):
                entries.append((name, "dir"))
            elif os.path.isfile(full):
                entries.append((name, "file"))
        return entries

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


#: Хранилище по умолчанию: состояния нет, один объект на процесс.
DEFAULT_AGENT_STORE = FileAgentStore()


def agent_store_of(source: Any) -> AgentStore:
    """Хранилище агента контекста; без него — файловое по умолчанию."""
    store = getattr(source, "agent_store", None)
    return store if store is not None else DEFAULT_AGENT_STORE


def read_resource_text(store: AgentStore, resource: AgentResource) -> str:
    """Текст ресурса либо ``ValueError`` с его именем — как ``read_user_text``."""
    try:
        return store.read_text(resource)
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(unreadable_reason(resource.label, exc)) from exc


def read_resource_or_none(
    store: AgentStore, resource: AgentResource
) -> str | None:
    """Текст ресурса либо ``None`` — как ``read_user_text_or_none``."""
    try:
        return store.read_text(resource)
    except (OSError, UnicodeDecodeError):
        return None


def read_required_text(
    store: AgentStore, resource: AgentResource, *, missing: str
) -> str:
    """Обязательный ресурс: нет — ``FileNotFoundError(missing)``, не читается —
    ``ValueError`` с его именем (проверка ``os.path.exists`` + ``read_user_text``)."""
    try:
        return store.read_text(resource)
    except FileNotFoundError:
        raise FileNotFoundError(missing) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(unreadable_reason(resource.label, exc)) from exc


def check_agent_store(store: Any) -> AgentStore:
    """Хранилище встраивающего — проверка на входе, а не на первом чтении."""
    if not isinstance(store, AgentStore):
        raise TypeError(
            "agent_store должен быть AgentStore (например FileAgentStore), "
            f"а не {type(store).__name__}"
        )
    return store


__all__ = [
    "DEFAULT_AGENT_STORE",
    "AgentResource",
    "AgentStore",
    "DirEntry",
    "FileAgentStore",
    "agent_store_of",
    "check_agent_store",
    "read_required_text",
    "read_resource_or_none",
    "read_resource_text",
]
