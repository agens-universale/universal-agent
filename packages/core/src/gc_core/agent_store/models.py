"""Модели хранилища агента: только данные, одинаковые для любых хранилищ."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

#: Вид ресурса агента. ``instructions`` — слои AGENTS.md ``L0`` и ``L1``
#: (``L2``, ``L3`` и скиллы проекта читаются из workspace, не отсюда).
ResourceKind = Literal["prompt", "instructions", "subagent", "skill"]


class Prompt(StrEnum):
    """Логические имена промптов."""

    SYSTEM = "system"
    SPECIAL_INSTRUCTIONS = "special_instructions"
    SUBAGENT = "subagent"
    CUSTOM_SUBAGENT = "custom_subagent"
    SUMMARY = "summary"
    MEMORY_PATCH = "memory_patch"
    LIMIT_SUMMARY = "limit_summary"
    INTENT_TOOL = "intent_tool"
    INTENT_FINAL = "intent_final"
    FINISH_GATE = "finish_gate"
    PROJECT_MEMORY_COMPACTION = "project_memory_compaction"
    COMMAND_JUDGE_SYSTEM = "command_judge_system"
    COMMAND_JUDGE_USER = "command_judge_user"
    INIT_AGENTS_MD = "init_agents_md"


@dataclass(frozen=True)
class ResourceInfo:
    """Запись каталога ресурсов: имя внутри каталога и вид."""

    name: str
    type: Literal["file", "dir"]
    version: str | None = None


@dataclass(frozen=True)
class ProjectKey:
    """Проект, чья память читается и пишется.

    ``id`` — устойчивое имя проекта (slug каталога): по нему память находит
    своё хранилище. ``root`` — каталог проекта: файловое хранилище держит
    память в ``<root>/.gc/project_memory.md``.
    """

    id: str
    root: str | None = None


@dataclass(frozen=True)
class ProjectMemory:
    """Память проекта и её версия (``None`` — пусто)."""

    text: str
    version: str | None


def memory_version(text: str) -> str | None:
    """Версия памяти: sha256 текста как есть; пустой и из одних пробелов —
    ``None`` (та же величина, что ``project_memory_content_hash`` в meta)."""
    if not text.strip():
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
