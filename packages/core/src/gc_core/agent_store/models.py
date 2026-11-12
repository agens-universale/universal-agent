"""Модели хранилища агента: только данные, одинаковые для любых хранилищ."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

#: Вид ресурса агента. ``instructions`` — слои AGENTS.md ``L0``–``L3``;
#: ``L2``, ``L3`` и слой скиллов ``project`` — ресурсы проекта.
ResourceKind = Literal["prompt", "instructions", "subagent", "skill"]

#: Слои AGENTS.md, которые принадлежат проекту.
PROJECT_INSTRUCTIONS = frozenset({"L2", "L3"})
#: Слой скиллов проекта.
PROJECT_SKILL_LAYER = "project"


def needs_project(kind: ResourceKind, name: str) -> bool:
    """Ресурс проекта (L2, L3, скиллы слоя ``project``): без проекта его не
    прочитать."""
    if kind == "instructions":
        return name in PROJECT_INSTRUCTIONS
    if kind == "skill":
        return name.strip("/").split("/", 1)[0] == PROJECT_SKILL_LAYER
    return False


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
    ROUTING_PAIR_JUDGE = "routing_pair_judge"
    PROJECT_MEMORY_COMPACTION = "project_memory_compaction"
    COMMAND_JUDGE_SYSTEM = "command_judge_system"
    COMMAND_JUDGE_USER = "command_judge_user"
    ACTION_JUDGE_SYSTEM = "action_judge_system"
    ACTION_JUDGE_USER = "action_judge_user"
    ACTION_JUDGE_EXTERNAL = "action_judge_external"
    INIT_AGENTS_MD = "init_agents_md"


#: Промпты, без которых ход падает посреди работы (сжатие, finish gate,
#: судьи). Специнструкции необязательны: их задаёт роль запуска.
REQUIRED_PROMPTS = tuple(
    p for p in Prompt if p is not Prompt.SPECIAL_INSTRUCTIONS
)


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
