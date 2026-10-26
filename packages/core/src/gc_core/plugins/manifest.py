"""Манифест плагина: каталог с skills и хуками (раскладка как у Claude Code)."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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

PLUGIN_FORMAT_CLAUDE = "claude-code"
PLUGIN_FORMAT_AP = "agent-plugins"
_CLAUDE_MANIFEST = ".claude-plugin/plugin.json"
_AP_MANIFEST = "plugin.json"

# Правила Agent Plugins v1.0.0 (спецификация §5 и plugin.schema.json);
# во время работы схема не запрашивается.
_AP_SCHEMA_ID = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
_AP_SCHEMA_KEY = "$schema"
_AP_REQUIRED = (_AP_SCHEMA_KEY, "name")
_AP_NAME_PATTERN = re.compile(
    r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$"
)
_AP_NAME_MAX = 64
_AP_FIELDS = frozenset(
    {
        _AP_SCHEMA_KEY,
        "name",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
        "extensions",
    }
)
_AP_STRING_FIELDS = (
    "version",
    "description",
    "homepage",
    "repository",
    "license",
)
_AP_AUTHOR_FIELDS = frozenset({"name", "email", "url"})
# Серверы MCP плагина Claude Code: ``.mcp.json``, иначе ``mcp.json``
# (у Agent Plugins только ``mcp.json``, см. ``_AP_MCP_FILE``).
_MCP_CANDIDATES = (".mcp.json", "mcp.json")
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
    # Те же серверы до подстановки путей и ``${VAR}`` (transport, command,
    # args, url как в файле): для сводки установки и сравнения при update.
    mcp_servers_raw: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Пропущенные серверы и битый mcp.json: имя и причина, без значений.
    mcp_problems: tuple[str, ...] = ()
    commands: tuple[PluginCommand, ...] = ()
    agents: tuple[PluginAgent, ...] = ()
    format: str = PLUGIN_FORMAT_CLAUDE

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


def is_agent_plugins_root(root: Path) -> bool:
    """Только корневой ``plugin.json`` без ``.claude-plugin/plugin.json``."""
    return (root / _AP_MANIFEST).is_file() and not (
        root / _CLAUDE_MANIFEST
    ).is_file()


def _ap_name_error(name: Any) -> str | None:
    ok = (
        isinstance(name, str)
        and 0 < len(name) <= _AP_NAME_MAX
        and _AP_NAME_PATTERN.match(name)
    )
    return None if ok else f"недопустимое имя плагина: {name!r}"


def _ap_type_errors(data: dict[str, Any]) -> list[str]:
    errors = [
        f"{k} должно быть строкой"
        for k in _AP_STRING_FIELDS
        if k in data and not isinstance(data[k], str)
    ]
    keywords = data.get("keywords", [])
    if not (
        isinstance(keywords, list)
        and all(isinstance(k, str) for k in keywords)
    ):
        errors.append("keywords должно быть списком строк")
    author = data.get("author", {})
    if not (
        isinstance(author, dict)
        and set(author) <= _AP_AUTHOR_FIELDS
        and all(isinstance(v, str) for v in author.values())
    ):
        errors.append("author: допустимы только строки name, email, url")
    return errors


def validate_ap_manifest(data: dict[str, Any]) -> list[str]:
    """Ошибки манифеста Agent Plugins 1.0.0; пустой список значит «верен».

    Неизвестные верхнеуровневые поля сюда не входят: они не фатальны
    (``load_plugin`` пишет о них предупреждение и игнорирует).
    """
    errors = [
        f"нет обязательного поля {k}" for k in _AP_REQUIRED if k not in data
    ]
    if _AP_SCHEMA_KEY in data and data[_AP_SCHEMA_KEY] != _AP_SCHEMA_ID:
        errors.append(f"{_AP_SCHEMA_KEY} должен быть {_AP_SCHEMA_ID}")
    if "name" in data:
        name_error = _ap_name_error(data["name"])
        if name_error:
            errors.append(name_error)
    return errors + _ap_type_errors(data)


def ap_manifest_errors(path: str | Path) -> list[str]:
    """Ошибки корневого ``plugin.json`` каталога Agent Plugins; иначе ``[]``."""
    root = Path(path).expanduser().resolve()
    if not is_agent_plugins_root(root):
        return []
    data = _read_json(root / _AP_MANIFEST)
    if data is None:
        return ["plugin.json не читается или не объект"]
    return validate_ap_manifest(data)


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


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _with_env(value: Any) -> Any:
    """Раскрыть ``${VAR}`` и ``${VAR:-по_умолчанию}`` из окружения во всей записи.

    Незаданная переменная без значения по умолчанию даёт ``KeyError`` с её именем.
    """
    if isinstance(value, str):

        def sub(m: re.Match[str]) -> str:
            found = os.environ.get(m.group(1), m.group(2))
            if found is None:
                raise KeyError(m.group(1))
            return found

        return _ENV_REF.sub(sub, value)
    if isinstance(value, list):
        return [_with_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _with_env(v) for k, v in value.items()}
    return value


_MCP_KEYS = ("command", "args", "env", "cwd", "url", "headers")
_MCP_TRANSPORTS = {"stdio", "stream_sse", "stream_http"}
# Наш ``http`` (на httpx) — разовый POST без рукопожатия streamable HTTP, а
# ``stream_http`` — клиент из SDK. В ``.mcp.json`` ``http`` означает streamable
# HTTP, поэтому оба типа плагина идут в ``stream_http``; ``sse`` — в клиент
# SDK ``stream_sse``.
_MCP_TYPE_ALIASES = {
    "streamable-http": "stream_http",
    "http": "stream_http",
    "sse": "stream_sse",
}
_MCP_SHOWN_KEYS = ("command", "args", "url")

# Разобранные серверы плагина: для запуска, для показа, пропущенные.
McpParse = tuple[
    dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[str]
]


def _mcp_shown(transport: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Запись как в файле, без подстановок: что показать и с чем сравнить."""
    return {
        "transport": transport,
        **{k: spec[k] for k in _MCP_SHOWN_KEYS if k in spec},
    }


def _mcp_transport(spec: dict[str, Any]) -> str:
    transport = str(
        spec.get("type") or ("stdio" if spec.get("command") else "stream_http")
    ).lower()
    return _MCP_TYPE_ALIASES.get(transport, transport)


def _mcp_entry(spec: dict[str, Any], root: Path) -> dict[str, Any]:
    """Поля ``McpServerConfig`` из записи ``.mcp.json`` с известным транспортом."""
    transport = _mcp_transport(spec)
    entry: dict[str, Any] = {"transport": transport}
    entry.update({k: spec[k] for k in _MCP_KEYS if k in spec})
    if transport == "stdio" and not entry.get("cwd"):
        entry["cwd"] = str(root)
    return entry


def _skip(problems: list[str], name: Any, reason: str) -> None:
    logger.warning("plugin mcp server %r skipped: %s", name, reason)
    problems.append(f"mcp {name}: пропущен ({reason})")


def _parse_mcp_servers(raw: Any, root: Path) -> McpParse:
    """``.mcp.json`` как у Claude Code: ``{"mcpServers": {имя: {...}}}`` или
    сразу ``{имя: {...}}``. ``${CLAUDE_PLUGIN_ROOT}`` подставляется путём
    плагина, ``${VAR}`` / ``${VAR:-def}`` берутся из окружения (сервер с
    незаданной ``${VAR}`` пропускается). Имя сервера становится
    ``<плагин>_<имя>``, чтобы плагины не затирали друг друга и серверы
    пользователя.
    """
    section = raw.get("mcpServers", raw) if isinstance(raw, dict) else None
    out: dict[str, dict[str, Any]] = {}
    shown: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for name, spec in (section if isinstance(section, dict) else {}).items():
        if not isinstance(spec, dict):
            _skip(problems, name, "entry is not an object")
            continue
        transport = _mcp_transport(spec)
        if transport not in _MCP_TRANSPORTS:
            _skip(problems, name, f"transport {transport!r} is not supported")
            continue
        try:
            expanded = _with_env(_with_plugin_root(spec, root))
        except KeyError as exc:
            # Сервер верный, переменная нужна при запуске агента: в сводке
            # установки он есть, в конфиг не попадает, пока её не задали.
            shown[str(name)] = _mcp_shown(transport, spec)
            _skip(problems, name, f"env var {exc.args[0]} is not set")
            continue
        out[str(name)] = _mcp_entry(expanded, root)
        shown[str(name)] = _mcp_shown(transport, spec)
    return out, shown, problems


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


# Agent Plugins v1.0.0, раздел MCP: ``mcp.json`` в корне плагина.
_AP_MCP_FILE = "mcp.json"
_AP_MCP_SCHEMA_ID = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
_AP_RESERVED_ENV = ("PLUGIN_ROOT", "PLUGIN_DATA")
_AP_PLACEHOLDER = re.compile(r"\$\{(PLUGIN_ROOT|PLUGIN_DATA)\}")
_AP_MCP_VARIANTS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    # type -> (обязательные поля, все допустимые поля)
    "stdio": (
        frozenset({"type", "command"}),
        frozenset({"type", "command", "args", "env", "cwd"}),
    ),
    "streamable-http": (
        frozenset({"type", "url"}),
        frozenset({"type", "url", "headers"}),
    ),
    "sse": (
        frozenset({"type", "url"}),
        frozenset({"type", "url", "headers"}),
    ),
}


def _ap_expand(value: str, root: Path, data: Path) -> str:
    """Один проход без рекурсии: ``${PLUGIN_ROOT}`` / ``${PLUGIN_DATA}``."""
    paths = {"PLUGIN_ROOT": str(root), "PLUGIN_DATA": str(data)}
    return _AP_PLACEHOLDER.sub(lambda m: paths[m.group(1)], value)


def _ap_inside(path: Path, base: Path) -> bool:
    """Внутри ``base`` и после разрешения symlink-ов."""
    return path.resolve().is_relative_to(base.resolve())


def _ap_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _ap_str_map(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    )


def _ap_url_error(spec: dict[str, Any]) -> str | None:
    url = spec["url"]
    if not isinstance(url, str):
        return "url is not a string"
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return "url is malformed"
    if parts.scheme not in ("http", "https") or not host:
        return "url is not an absolute http(s) URL"
    if parts.username is not None or parts.password is not None:
        return "url has user information"
    if parts.fragment:
        return "url has a fragment"
    if parts.scheme == "http":
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host.lower() == "localhost"
        if not loopback:
            return "non-loopback url must use https"
    headers = spec.get("headers", {})
    if not _ap_str_map(headers):
        return "headers must be a string map"
    names = [k.lower() for k in headers]
    if len(names) != len(set(names)):
        return "duplicate header names"
    return None


def _ap_command(command: Any, root: Path) -> tuple[str | None, str | None]:
    if not isinstance(command, str) or not command:
        return None, "command is not a non-empty string"
    if command.startswith("./"):
        resolved = root / command
        if not _ap_inside(resolved, root):
            return None, "command escapes the plugin root"
        return str(resolved), None
    if "/" in command or "\\" in command or any(c.isspace() for c in command):
        return None, "command must be one token: a bare name or ./path"
    return command, None


def _ap_cwd(
    cwd: Any, root: Path, data: Path
) -> tuple[Path | None, str | None]:
    if cwd is None:
        return root, None
    if not isinstance(cwd, str):
        return None, "cwd is not a string"
    if cwd.startswith("./"):
        path, base = root / cwd, root
    else:
        head = re.match(r"\$\{(PLUGIN_ROOT|PLUGIN_DATA)\}(?:/|$)", cwd)
        if head is None:
            return None, "cwd must be ./relative or start with ${PLUGIN_*}"
        path = Path(_ap_expand(cwd, root, data))
        base = root if head.group(1) == "PLUGIN_ROOT" else data
    if not _ap_inside(path, base):
        return None, "cwd escapes its base directory"
    return path, None


def _ap_stdio_entry(
    spec: dict[str, Any], root: Path, plugin_name: str
) -> tuple[dict[str, Any] | None, str | None]:
    from gc_core.plugins.installed import plugin_data_path

    # Каталог данных создаёт ``runtime.plugin_mcp_servers`` при загрузке
    # настроек, не здесь: чтение плагина (prepare, /plugins) ничего не пишет.
    data = plugin_data_path(plugin_name)
    args, env = spec.get("args", []), spec.get("env", {})
    if not _ap_str_list(args) or not _ap_str_map(env):
        return None, "args/env have wrong types"
    if any(k in env for k in _AP_RESERVED_ENV):
        return None, "env defines a reserved name (PLUGIN_ROOT/PLUGIN_DATA)"
    command, reason = _ap_command(spec["command"], root)
    cwd, cwd_reason = _ap_cwd(spec.get("cwd"), root, data)
    if command is None or cwd is None:
        return None, reason or cwd_reason
    return {
        "transport": "stdio",
        "command": command,
        "args": [_ap_expand(a, root, data) for a in args],
        "env": {
            **{k: _ap_expand(v, root, data) for k, v in env.items()},
            "PLUGIN_ROOT": str(root),
            "PLUGIN_DATA": str(data),
        },
        "cwd": str(cwd),
    }, None


def _ap_server_entry(
    spec: Any, root: Path, plugin_name: str
) -> tuple[dict[str, Any] | None, str | None]:
    variant = _AP_MCP_VARIANTS.get(
        spec.get("type") if isinstance(spec, dict) else None
    )
    if variant is None:
        return None, "unknown type"
    if not variant[0] <= set(spec) <= variant[1]:
        return None, "fields do not fit the server variant"
    if spec["type"] == "stdio":
        return _ap_stdio_entry(spec, root, plugin_name)
    reason = _ap_url_error(spec)
    if reason is not None:
        return None, reason
    entry: dict[str, Any] = {
        "transport": _MCP_TYPE_ALIASES.get(spec["type"], spec["type"]),
        "url": spec["url"],
    }
    if "headers" in spec:
        entry["headers"] = dict(spec["headers"])
    return entry, None


def _ap_mcp_top_error(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return "not a JSON object or bad JSON"
    if raw.get(_AP_SCHEMA_KEY) != _AP_MCP_SCHEMA_ID:
        return "missing or wrong $schema"
    if set(raw) != {_AP_SCHEMA_KEY, "mcpServers"}:
        return "unexpected top-level key"
    if not isinstance(raw["mcpServers"], dict):
        return "mcpServers is not an object"
    return None


def _parse_ap_mcp_servers(raw: Any, root: Path, plugin_name: str) -> McpParse:
    """``mcp.json`` по Agent Plugins v1.0.0: ``$schema`` и ``mcpServers``, у
    сервера ровно один вариант (stdio / streamable-http / sse). Раскрываются
    только ``${PLUGIN_ROOT}`` и ``${PLUGIN_DATA}`` в ``args``, ``env``,
    ``cwd``; остальное, включая ``${VAR}``, остаётся буквальным. Невалидная
    запись пропускается с предупреждением (имя и причина, без значений).
    """
    top_error = _ap_mcp_top_error(raw)
    if top_error:
        logger.warning(
            "plugin %s: mcp.json is invalid (%s), MCP disabled",
            root,
            top_error,
        )
        return {}, {}, [f"mcp.json не принят ({top_error})"]
    out: dict[str, dict[str, Any]] = {}
    shown: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for name, spec in raw["mcpServers"].items():
        entry, reason = _ap_server_entry(spec, root, plugin_name)
        if entry is None:
            _skip(problems, name, str(reason))
            continue
        out[str(name)] = entry
        shown[str(name)] = _mcp_shown(entry["transport"], spec)
    return out, shown, problems


def _load_mcp_servers(root: Path, fmt: str, name: str) -> McpParse:
    """Серверы MCP плагина: ``mcp.json`` для Agent Plugins, ``.mcp.json`` /
    ``mcp.json`` для Claude Code."""
    if fmt == PLUGIN_FORMAT_AP:
        path = root / _AP_MCP_FILE
        if not path.is_file():
            return {}, {}, []
        return _parse_ap_mcp_servers(_read_json(path), root, name)
    path = next(
        (root / rel for rel in _MCP_CANDIDATES if (root / rel).is_file()), None
    )
    return _parse_mcp_servers(_read_json(path), root) if path else ({}, {}, [])


_AP_IGNORED = (_HOOKS_FILE, "commands", "agents")


def _cc_components(
    root: Path, fmt: str
) -> tuple[
    dict[str, tuple[HookMatcher, ...]],
    tuple[PluginCommand, ...],
    tuple[PluginAgent, ...],
]:
    """Хуки, команды и агенты; у Agent Plugins их нет (только skills и MCP)."""
    if fmt == PLUGIN_FORMAT_AP:
        ignored = [rel for rel in _AP_IGNORED if (root / rel).exists()]
        if ignored:
            logger.info(
                "plugin %s: Agent Plugins format, ignored: %s",
                root,
                ", ".join(ignored),
            )
        return {}, (), ()
    hooks_path = root / _HOOKS_FILE
    hooks = (
        _parse_hooks(_read_json(hooks_path)) if hooks_path.is_file() else {}
    )
    return hooks, _load_commands(root), _load_agents(root)


def load_plugin(path: str | Path) -> Plugin | None:
    """Прочитать каталог плагина. ``None``, если это не плагин."""
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        logger.warning("plugin path %s is not a directory", root)
        return None
    manifest: dict[str, Any] = {}
    fmt = PLUGIN_FORMAT_CLAUDE
    if is_agent_plugins_root(root):
        fmt = PLUGIN_FORMAT_AP
        errors = ap_manifest_errors(root)
        if errors:
            logger.warning(
                "plugin %s: manifest is invalid, skipped: %s",
                root,
                "; ".join(errors),
            )
            return None
        manifest = _read_json(root / _AP_MANIFEST) or {}
        for key in sorted(set(manifest) - _AP_FIELDS):
            logger.warning(
                "plugin %s: unknown manifest field %r ignored", root, key
            )
    elif (root / _CLAUDE_MANIFEST).is_file():
        manifest = _read_json(root / _CLAUDE_MANIFEST) or {}
    skills_dir = root / "skills"
    hooks, commands, agents = _cc_components(root, fmt)
    name = str(manifest.get("name") or root.name)
    version = str(manifest.get("version") or "0")
    mcp_servers, mcp_shown, mcp_problems = _load_mcp_servers(root, fmt, name)
    return Plugin(
        name=name,
        version=version,
        root=root,
        skills_dir=skills_dir if skills_dir.is_dir() else None,
        hooks=hooks,
        mcp_servers=mcp_servers,
        mcp_servers_raw=mcp_shown,
        mcp_problems=tuple(mcp_problems),
        commands=commands,
        agents=agents,
        format=fmt,
    )
