"""Манифест плагина: каталог с skills и хуками (раскладка как у Claude Code)."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

EVENT_SESSION_START = "SessionStart"
EVENT_PRE_TOOL_USE = "PreToolUse"
EVENT_POST_TOOL_USE = "PostToolUse"
EVENT_POST_TOOL_USE_FAILURE = "PostToolUseFailure"
EVENT_USER_PROMPT_SUBMIT = "UserPromptSubmit"
EVENT_STOP = "Stop"
SUPPORTED_EVENTS = frozenset(
    {
        EVENT_SESSION_START,
        EVENT_PRE_TOOL_USE,
        EVENT_POST_TOOL_USE,
        EVENT_POST_TOOL_USE_FAILURE,
        EVENT_USER_PROMPT_SUBMIT,
        EVENT_STOP,
    }
)

_MANIFEST_CANDIDATES = (".claude-plugin/plugin.json", "plugin.json")
_HOOKS_FILE = "hooks/hooks.json"


@dataclass(frozen=True)
class HookCommand:
    command: str
    timeout_sec: int | None = None


@dataclass(frozen=True)
class HookMatcher:
    """Условие запуска группы хуков.

    ``*`` или пустая строка подходят всегда. Иначе это регулярное выражение,
    которое должно совпасть со значением целиком: для ``SessionStart`` это
    источник старта (``startup``), для событий инструментов имя инструмента.
    """

    matcher: str
    hooks: tuple[HookCommand, ...]

    def matches(self, value: str | None) -> bool:
        if self.matcher in ("", "*"):
            return True
        if value is None:
            return False
        try:
            return re.fullmatch(self.matcher, value) is not None
        except re.error:
            return False


@dataclass(frozen=True)
class PluginCommand:
    """Команда плагина: ``commands/<имя>.md`` (текст запроса под коротким именем).

    ``$ARGUMENTS`` в тексте заменяется аргументами, ``$1``..``$9`` словами
    аргументов (как у Claude Code).
    """

    name: str
    description: str
    argument_hint: str
    allowed_tools: tuple[str, ...]
    body: str
    source: str


@dataclass(frozen=True)
class PluginAgent:
    """Субагент плагина: ``agents/<имя>.md`` (шапка YAML и текст роли).

    ``tools`` это имена инструментов из шапки как есть (в именах Claude Code
    либо агента); ``None``, если ключа нет. Перевод в имена агента делает
    ``plugins.agents``.
    """

    name: str
    description: str
    tools: tuple[str, ...] | None
    body: str
    source: str


@dataclass(frozen=True)
class Plugin:
    name: str
    version: str
    root: Path
    skills_dir: Path | None
    hooks: dict[str, tuple[HookMatcher, ...]] = field(default_factory=dict)
    # Серверы из ``.mcp.json`` плагина: имя -> поля McpServerConfig.
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    commands: tuple[PluginCommand, ...] = ()
    agents: tuple[PluginAgent, ...] = ()

    def hooks_for(self, event: str, value: str | None) -> list[HookCommand]:
        out: list[HookCommand] = []
        for group in self.hooks.get(event, ()):
            if group.matches(value):
                out.extend(group.hooks)
        return out


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("plugin file %s unreadable: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def _hook_command(item: Any) -> HookCommand | None:
    """Одна запись ``{"type": "command", "command": "..."}`` или ``None``."""
    if not isinstance(item, dict) or item.get("type", "command") != "command":
        return None
    command = item.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    timeout = item.get("timeout")
    valid = isinstance(timeout, int) and timeout > 0
    return HookCommand(command=command, timeout_sec=timeout if valid else None)


def _hook_group(group: Any) -> HookMatcher | None:
    """Группа ``{"matcher": ..., "hooks": [...]}``; без команд ``None``."""
    if not isinstance(group, dict):
        return None
    commands = [_hook_command(i) for i in group.get("hooks") or []]
    found = tuple(c for c in commands if c is not None)
    if not found:
        return None
    return HookMatcher(matcher=str(group.get("matcher") or ""), hooks=found)


def _event_matchers(groups: Any) -> tuple[HookMatcher, ...]:
    if not isinstance(groups, list):
        return ()
    found = (_hook_group(g) for g in groups)
    return tuple(m for m in found if m is not None)


def _parse_hooks(raw: Any) -> dict[str, tuple[HookMatcher, ...]]:
    if not isinstance(raw, dict):
        return {}
    section = raw.get("hooks", raw)
    if not isinstance(section, dict):
        return {}
    parsed: dict[str, tuple[HookMatcher, ...]] = {}
    for event, groups in section.items():
        if event not in SUPPORTED_EVENTS:
            logger.info(
                "plugin hook event %r is not supported, skipped", event
            )
            continue
        matchers = _event_matchers(groups)
        if matchers:
            parsed[event] = matchers
    return parsed


def _with_plugin_root(value: Any, root: Path) -> Any:
    """Подставить путь плагина вместо ``${CLAUDE_PLUGIN_ROOT}`` во всей записи."""
    if isinstance(value, str):
        return value.replace("${CLAUDE_PLUGIN_ROOT}", str(root)).replace(
            "${GC_PLUGIN_ROOT}", str(root)
        )
    if isinstance(value, list):
        return [_with_plugin_root(v, root) for v in value]
    if isinstance(value, dict):
        return {k: _with_plugin_root(v, root) for k, v in value.items()}
    return value


_MCP_KEYS = ("command", "args", "env", "cwd", "url", "headers")
_MCP_TRANSPORTS = {"stdio", "http", "sse"}


def _mcp_entry(
    name: Any, spec: dict[str, Any], root: Path
) -> dict[str, Any] | None:
    """Поля ``McpServerConfig`` из записи ``.mcp.json``; ``None``, если транспорт чужой."""
    transport = str(
        spec.get("type") or ("stdio" if spec.get("command") else "http")
    ).lower()
    if transport not in _MCP_TRANSPORTS:
        logger.info(
            "plugin mcp server %r: transport %r skipped", name, transport
        )
        return None
    entry: dict[str, Any] = {"transport": transport}
    entry.update({k: spec[k] for k in _MCP_KEYS if k in spec})
    if transport == "stdio" and not entry.get("cwd"):
        entry["cwd"] = str(root)
    return entry


def _parse_mcp_servers(raw: Any, root: Path) -> dict[str, dict[str, Any]]:
    """``.mcp.json`` как у Claude Code: ``{"mcpServers": {имя: {...}}}`` или
    сразу ``{имя: {...}}``. ``${CLAUDE_PLUGIN_ROOT}`` подставляется путём
    плагина. Имя сервера становится ``<плагин>_<имя>``, чтобы плагины не
    затирали друг друга и серверы пользователя.
    """
    section = raw.get("mcpServers", raw) if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, spec in section.items():
        if not isinstance(spec, dict):
            continue
        entry = _mcp_entry(name, _with_plugin_root(spec, root), root)
        if entry is not None:
            out[str(name)] = entry
    return out


def _split_markdown(text: str) -> tuple[dict[str, Any], str]:
    """YAML-шапка и тело ``.md``; без шапки (или с битой) шапка пустая."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()
    end = next(
        (i for i in range(1, len(lines)) if lines[i].strip() == "---"), None
    )
    if end is None:
        return {}, text.strip()
    try:
        meta = yaml.safe_load("".join(lines[1:end]))
    except yaml.YAMLError:
        meta = None
    body = "".join(lines[end + 1 :]).strip()
    return (meta if isinstance(meta, dict) else {}), body


def _names(value: Any) -> tuple[str, ...] | None:
    """Список имён из строки через запятую или из списка; ``None``, если пусто."""
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, list):
        items = [v for v in value if isinstance(v, str)]
    else:
        return None
    out = tuple(dict.fromkeys(i.strip() for i in items if i.strip()))
    return out or None


def _markdown_files(directory: Path) -> list[Path]:
    """Файлы ``*.md`` каталога (без вложенных), по имени."""
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob("*.md") if p.is_file())


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        logger.warning("plugin file %s unreadable: %s", path, exc)
        return None


def _load_commands(root: Path) -> tuple[PluginCommand, ...]:
    out: list[PluginCommand] = []
    for path in _markdown_files(root / "commands"):
        text = _read_text(path)
        if text is None:
            continue
        meta, body = _split_markdown(text)
        if not body:
            logger.warning("plugin command %s is empty, skipped", path)
            continue
        out.append(
            PluginCommand(
                name=path.stem,
                description=str(meta.get("description") or "").strip(),
                argument_hint=str(meta.get("argument-hint") or "").strip(),
                allowed_tools=_names(meta.get("allowed-tools")) or (),
                body=body,
                source=str(path),
            )
        )
    return tuple(out)


def _load_agents(root: Path) -> tuple[PluginAgent, ...]:
    out: list[PluginAgent] = []
    for path in _markdown_files(root / "agents"):
        text = _read_text(path)
        if text is None:
            continue
        meta, body = _split_markdown(text)
        if not body:
            logger.warning("plugin agent %s has no role text, skipped", path)
            continue
        out.append(
            PluginAgent(
                name=str(meta.get("name") or path.stem).strip(),
                description=str(meta.get("description") or "").strip(),
                tools=_names(meta.get("tools")),
                body=body,
                source=str(path),
            )
        )
    return tuple(out)


def load_plugin(path: str | Path) -> Plugin | None:
    """Прочитать каталог плагина. ``None``, если это не плагин."""
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        logger.warning("plugin path %s is not a directory", root)
        return None
    manifest: dict[str, Any] = {}
    for rel in _MANIFEST_CANDIDATES:
        candidate = root / rel
        if candidate.is_file():
            manifest = _read_json(candidate) or {}
            break
    skills_dir = root / "skills"
    hooks_path = root / _HOOKS_FILE
    hooks = (
        _parse_hooks(_read_json(hooks_path)) if hooks_path.is_file() else {}
    )
    name = str(manifest.get("name") or root.name)
    version = str(manifest.get("version") or "0")
    mcp_path = root / ".mcp.json"
    mcp_servers = (
        _parse_mcp_servers(_read_json(mcp_path), root)
        if mcp_path.is_file()
        else {}
    )
    return Plugin(
        name=name,
        version=version,
        root=root,
        skills_dir=skills_dir if skills_dir.is_dir() else None,
        hooks=hooks,
        mcp_servers=mcp_servers,
        commands=_load_commands(root),
        agents=_load_agents(root),
    )
