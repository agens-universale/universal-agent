"""Где файловое хранилище агента берёт ресурс: логическое имя → путь.

Правила — те же, что были в местах чтения до хранилища v2: путь из
настроек (с runtime-слоем фронтенда), иначе пакетный файл. Несколько путей —
цепочка: берётся первый, что читается и не пуст (промпт субагента: свой из
конфига, иначе пакетный).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gc_core.agent_store.models import Prompt, ResourceKind
from gc_core.bundled_assets import bundled_root

_AGENTS_MD = "AGENTS.md"


def _bundled_prompt(*parts: str) -> str:
    return str(Path(bundled_root(), "prompts", *parts))


def _configured(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _under_bundled(value: Any, default: str) -> str:
    """Путь конфига; относительный — от корня пакетных ассетов."""
    raw = str(value or default).strip()
    if not raw:
        raise ValueError(f"путь промпта пуст (по умолчанию {default})")
    if os.path.isabs(raw):
        return raw
    return os.path.abspath(os.path.join(str(bundled_root()), raw))


def _only(path: str | None) -> list[str]:
    return [path] if path else []


def _system(s) -> list[str]:
    return _only(_configured(s.paths.chat_completions_system_prompt_path))


def _special(s) -> list[str]:
    return _only(
        _configured(s.paths.chat_completions_special_instructions_path)
    )


def _subagent(_s) -> list[str]:
    return [_bundled_prompt("chat_completions_subagent_system_prompt.md")]


def _custom_subagent(s) -> list[str]:
    own = _configured(
        s.paths.chat_completions_custom_subagent_system_prompt_path
    )
    bundled = _bundled_prompt(
        "chat_completions_custom_subagent_system_prompt.md"
    )
    return [os.path.abspath(own), bundled] if own else [bundled]


def _summary(s) -> list[str]:
    default = _bundled_prompt("chat_completions_summary_prompt.md")
    return [str(s.summary.chat_completions_summary_prompt_path or default)]


def _memory_patch(s) -> list[str]:
    return [
        _under_bundled(
            s.summary.chat_completions_memory_patch_replace_prompt_path,
            _bundled_prompt("chat_completions_memory_patch_replace.md"),
        )
    ]


def _intent(attr: str, file_name: str) -> Callable[[Any], list[str]]:
    def paths(s) -> list[str]:
        raw = str(getattr(s.intent_scope, attr) or "").strip()
        return [_under_bundled(raw, _bundled_prompt(file_name))]

    return paths


def _finish_gate(s) -> list[str]:
    return [
        s.paths.chat_completions_intent_guard_prompt_path
        or _bundled_prompt("chat_completions_intent_guard_prompt.md")
    ]


def _bundled(*parts: str) -> Callable[[Any], list[str]]:
    return lambda _s: [_bundled_prompt(*parts)]


_PROMPTS: dict[str, Callable[[Any], list[str]]] = {
    Prompt.SYSTEM: _system,
    Prompt.SPECIAL_INSTRUCTIONS: _special,
    Prompt.SUBAGENT: _subagent,
    Prompt.CUSTOM_SUBAGENT: _custom_subagent,
    Prompt.SUMMARY: _summary,
    Prompt.MEMORY_PATCH: _memory_patch,
    Prompt.LIMIT_SUMMARY: _bundled("limit_trace_summary.md"),
    Prompt.INTENT_TOOL: _intent(
        "intent_scope_tool_summary_prompt_path",
        "intent_scope_tool_summary_prompt.md",
    ),
    Prompt.INTENT_FINAL: _intent(
        "intent_scope_final_answer_summary_prompt_path",
        "intent_scope_final_answer_summary_prompt.md",
    ),
    Prompt.FINISH_GATE: _finish_gate,
    Prompt.PROJECT_MEMORY_COMPACTION: _bundled(
        "project_memory_compaction_prompt.md"
    ),
    Prompt.COMMAND_JUDGE_SYSTEM: _bundled(
        "security", "command_judge_system.md"
    ),
    Prompt.COMMAND_JUDGE_USER: _bundled("security", "command_judge_user.md"),
    Prompt.INIT_AGENTS_MD: _bundled("init_agents_md_prompt.md"),
}


def _instructions(settings, name: str) -> list[str]:
    if name == "L0":
        own = settings.paths.agent_md_path
        return [str(Path(own))] if own else []
    if name == "L1":
        from gc_core.instructions.layers import instruction_file_paths

        return [str(instruction_file_paths()["L1"])]
    raise ValueError(f"неизвестный слой инструкций {name!r}")


def _subagent_definition(settings, name: str) -> list[str]:
    raw = settings.agent.subagents_dir
    base = str(raw).strip() if raw is not None else ""
    if not base:
        return []
    return [os.path.join(base, name) if name else base]


def skill_roots() -> dict[str, str]:
    """Корни слоёв скиллов файлового хранилища."""
    from gc_core.tools.skills.registry import (
        _project_skills_root,
        _system_skills_root,
        _user_skills_root,
    )

    return {
        "system": _system_skills_root(),
        "user": _user_skills_root(),
        "project": _project_skills_root(),
    }


def _skill(name: str) -> list[str]:
    layer, _, rest = name.strip("/").partition("/")
    root = skill_roots().get(layer)
    if root is None:
        return []
    return [os.path.join(root, *rest.split("/")) if rest else root]


def resource_paths(settings: Any, kind: ResourceKind, name: str) -> list[str]:
    """Пути ресурса по порядку; пусто — у файлового хранилища его нет."""
    if kind == "prompt":
        try:
            rule = _PROMPTS[Prompt(name)]
        except ValueError:
            raise ValueError(f"неизвестный промпт {name!r}") from None
        return rule(settings)
    if kind == "instructions":
        return _instructions(settings, name)
    if kind == "subagent":
        return _subagent_definition(settings, name)
    if kind == "skill":
        return _skill(name)
    raise ValueError(f"неизвестный вид ресурса {kind!r}")
