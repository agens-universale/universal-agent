"""Команды плагинов: разбор ``/плагин:команда аргументы`` и подстановка (§4.2).

Команда — шаблон запроса: ``$ARGUMENTS`` заменяется аргументами, ``$1``..``$9``
словами аргументов. Имя ``/имя`` разрешается одной функцией по объединению
команд плагинов и skills: её зовут и цикл агента, и TUI. Короткое имя
работает, только если однозначно по обоим спискам; при равных полных id
skill главнее (сборка снимка такие команды уже отбросила).
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from gc_core.message_policy.tool_usage_notice.policy import (
    wrap_thread_memory_technical_user_message,
)
from gc_core.message_policy.user_message_policy import (
    extract_real_user_message_text,
)
from gc_core.plugins.manifest import PluginCommand
from gc_core.plugins.snapshot import PluginSnapshot
from gc_core.settings import runtime_of

_SLASH_RE = re.compile(r"^/([\w.-]+(?::[\w.-]+)?)(?:\s+(.*))?$", re.DOTALL)
_POSITIONAL_RE = re.compile(r"\$([1-9])")


@dataclass(frozen=True)
class Resolution:
    """Итог разрешения ``/имя``: ``canonical`` — полный id, если он один;
    ``candidates`` — все подходящие полные id по алфавиту."""

    kind: Literal["command", "skill", "ambiguous", "none"]
    canonical: str
    candidates: tuple[str, ...]


def _matches(token: str, full_id: str) -> bool:
    if full_id == token:
        return True
    return ":" not in token and full_id.rpartition(":")[2] == token


def resolve_slash_name(
    token: str,
    commands: Sequence[tuple[str, PluginCommand]],
    skill_ids: Sequence[str],
) -> Resolution:
    """Полное имя или короткое, если оно однозначно по командам и skills."""
    skills = {s for s in skill_ids if _matches(token, s)}
    found = {
        full
        for full in (f"{plugin}:{cmd.name}" for plugin, cmd in commands)
        if _matches(token, full)
    }
    candidates = tuple(sorted(skills | found))
    if not candidates:
        return Resolution("none", "", ())
    if len(candidates) > 1:
        return Resolution("ambiguous", "", candidates)
    kind = "skill" if skills else "command"
    return Resolution(kind, candidates[0], candidates)


def expand_command(command: PluginCommand, arguments: str) -> str:
    """Текст команды с ``$ARGUMENTS`` и ``$1``..``$9`` вместо аргументов."""
    try:
        words = shlex.split(arguments)
    except ValueError:
        words = arguments.split()

    def positional(match: re.Match[str]) -> str:
        index = int(match.group(1)) - 1
        return words[index] if index < len(words) else ""

    body = command.body.replace("$ARGUMENTS", arguments)
    return _POSITIONAL_RE.sub(positional, body)


def _resolve_prompt(
    snapshot: PluginSnapshot, skill_ids: Sequence[str], prompt: str
) -> tuple[Resolution, str] | None:
    """Разрешение имени запроса ``/имя аргументы`` и аргументы; ``None`` —
    запрос не такой или команд в наборе нет."""
    match = _SLASH_RE.match(prompt.strip())
    if match is None or not snapshot.commands:
        return None
    resolution = resolve_slash_name(
        match.group(1), snapshot.commands, skill_ids
    )
    return resolution, (match.group(2) or "").strip()


def _command(snapshot: PluginSnapshot, full_id: str) -> PluginCommand:
    return next(
        cmd
        for plugin, cmd in snapshot.commands
        if f"{plugin}:{cmd.name}" == full_id
    )


def expanded_prompt(
    snapshot: PluginSnapshot, skill_ids: Sequence[str], prompt: str
) -> str | None:
    """Раскрытый текст, если запрос — команда плагина, иначе ``None``."""
    found = _resolve_prompt(snapshot, skill_ids, prompt)
    if found is None or found[0].kind != "command":
        return None
    resolution, arguments = found
    return expand_command(_command(snapshot, resolution.canonical), arguments)


def _ambiguity_notice(
    snapshot: PluginSnapshot, resolution: Resolution
) -> str | None:
    command_ids = {f"{p}:{c.name}" for p, c in snapshot.commands}
    if not command_ids.intersection(resolution.candidates):
        return None
    names = ", ".join(resolution.candidates)
    return (
        f"[команда плагина] Имя неоднозначно: {names}. Команда не "
        "подставлена; попроси пользователя указать полное имя."
    )


def _command_text(
    snapshot: PluginSnapshot, resolution: Resolution, arguments: str
) -> str | None:
    if resolution.kind == "ambiguous":
        return _ambiguity_notice(snapshot, resolution)
    if resolution.kind != "command":
        return None
    command = _command(snapshot, resolution.canonical)
    return (
        f"[команда плагина {resolution.canonical}] Пользователь вызвал "
        "команду. Выполни её инструкции для его запроса.\n\n"
        + expand_command(command, arguments)
    )


def command_messages(
    snapshot: PluginSnapshot, skill_ids: Sequence[str], prompt: str
) -> list[dict[str, Any]]:
    """Техническое сообщение к запросу ``/команда …``: раскрытая команда или,
    если имя неоднозначно и среди вариантов есть команда, причина; иначе
    пусто — запрос уходит модели как есть."""
    found = _resolve_prompt(snapshot, skill_ids, prompt)
    text = None if found is None else _command_text(snapshot, *found)
    if text is None:
        return []
    return [
        wrap_thread_memory_technical_user_message(
            text, message_id_suffix="plugin_command"
        )
    ]


def listed_skill_ids(skills: Sequence[Mapping[str, Any]]) -> list[str]:
    """Id skills из записей ``list_skills`` (для ``resolve_slash_name``)."""
    return [str(s["id"]) for s in skills if s.get("id")]


def user_intent_text(ctx: Any, state: Any, message: Mapping[str, Any]) -> str:
    """Текст реплики для лога намерений; запрос-команда плагина —
    раскрытым текстом (§4.2)."""
    text = extract_real_user_message_text(message)
    skill_ids = listed_skill_ids(getattr(state, "skills_list", None) or [])
    return expanded_prompt(runtime_of(ctx).plugins, skill_ids, text) or text
