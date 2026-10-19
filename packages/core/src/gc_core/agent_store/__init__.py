"""Хранилище агента v2: контракт, модели, файловое хранилище и чтение ядром.

Контракт — :class:`AgentStore` (``store.py``); по умолчанию —
:class:`FileAgentStore` (пути по настройкам, как до v2). Своё хранилище
(база, S3, конфиг-сервис) отвечает по виду и логическому имени.
"""

from gc_core.agent_store.errors import MemoryConflict
from gc_core.agent_store.file_store import FileAgentStore
from gc_core.agent_store.models import (
    ProjectKey,
    ProjectMemory,
    Prompt,
    ResourceInfo,
    ResourceKind,
    memory_version,
    needs_project,
)
from gc_core.agent_store.reading import (
    agent_store_of,
    aread_or_none,
    aread_required,
    aread_text,
    check_agent_store,
    decode_text,
    project_key_for,
    project_key_of,
    require_project,
)
from gc_core.agent_store.store import AgentStore

__all__ = [
    "AgentStore",
    "FileAgentStore",
    "MemoryConflict",
    "ProjectKey",
    "ProjectMemory",
    "Prompt",
    "ResourceInfo",
    "ResourceKind",
    "agent_store_of",
    "aread_or_none",
    "aread_required",
    "aread_text",
    "check_agent_store",
    "decode_text",
    "memory_version",
    "needs_project",
    "project_key_for",
    "project_key_of",
    "require_project",
]
