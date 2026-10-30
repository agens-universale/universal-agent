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
EVENT_USER_PROMPT_SUBMIT = "UserPromptSubmit"
SUPPORTED_EVENTS = frozenset({EVENT_SESSION_START, EVENT_USER_PROMPT_SUBMIT})
# Хуки на инструменты и Stop переедут на middleware (спека §7.3); до тех пор
# они пропускаются с диагностикой в ``Plugin.hook_problems``.
DEFERRED_EVENTS = frozenset(
    {"PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop"}
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
    источник старта (``startup`` или ``compact``).
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
    либо агента); ``None`` только если шапка разобрана и ключа в ней нет.
    Ключ есть, но пуст, ``null``, неверного типа или шапка не разобралась:
    ``()``, причина в ``problem``. Перевод в имена агента делает
    ``plugins.agents``.
    """

    name: str
    description: str
    tools: tuple[str, ...] | None
    body: str
    source: str
    problem: str = ""


@dataclass(frozen=True)
class Plugin:
    name: str
    version: str
    root: Path
    skills_dirs: tuple[Path, ...]
    hooks: dict[str, tuple[HookMatcher, ...]] = field(default_factory=dict)
    # Серверы из ``.mcp.json`` плагина: имя -> поля McpServerConfig.
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Те же серверы до подстановки путей и ``${VAR}`` (transport, command,
    # args, url как в файле): для сводки установки и сравнения при update.
    mcp_servers_raw: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Пропущенные серверы и битый mcp.json: имя и причина, без значений.
    mcp_problems: tuple[str, ...] = ()
    # Пропущенные хуки (события, которые цикл пока не запускает).
    hook_problems: tuple[str, ...] = ()
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


HookParse = tuple[dict[str, tuple[HookMatcher, ...]], tuple[str, ...]]


def _parse_hooks(raw: Any) -> HookParse:
    """Хуки поддержанных событий и диагностика пропущенных."""
    section = raw.get("hooks", raw) if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        return {}, ()
    parsed: dict[str, tuple[HookMatcher, ...]] = {}
    problems: list[str] = []
    for event, groups in section.items():
        if event in DEFERRED_EVENTS:
            problems.append(f"хуки {event} будут поддержаны после middleware")
        elif event not in SUPPORTED_EVENTS:
            logger.info(
                "plugin hook event %r is not supported, skipped", event
            )
        elif matchers := _event_matchers(groups):
            parsed[event] = matchers
    return parsed, tuple(problems)


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


def _mcp_shown(
    transport: str, spec: dict[str, Any], env_vars: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Запись как в файле, без подстановок: что показать и с чем сравнить.

    ``env_vars`` — переменные окружения, которые запись читает: плагин может
    передать любую из них на свой сервер, поэтому их видно в сводке.
    """
    shown = {
        "transport": transport,
        **{k: spec[k] for k in _MCP_SHOWN_KEYS if k in spec},
    }
    if env_vars:
        shown["env_vars"] = list(env_vars)
    return shown


_PLUGIN_ROOT_VARS = frozenset({"CLAUDE_PLUGIN_ROOT", "GC_PLUGIN_ROOT"})


def _strings(value: Any) -> list[str]:
    """Все строки записи (значения во вложенных списках и словарях)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def _env_refs(value: Any) -> tuple[str, ...]:
    """Имена ``${VAR}`` во всей записи (кроме пути плагина), по порядку."""
    names = (m.group(1) for s in _strings(value) for m in _ENV_REF.finditer(s))
    return tuple(dict.fromkeys(n for n in names if n not in _PLUGIN_ROOT_VARS))


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
            shown[str(name)] = _mcp_shown(transport, spec, _env_refs(spec))
            _skip(problems, name, f"env var {exc.args[0]} is not set")
            continue
        out[str(name)] = _mcp_entry(expanded, root)
        shown[str(name)] = _mcp_shown(transport, spec, _env_refs(spec))
    return out, shown, problems


def _split_markdown(text: str) -> tuple[dict[str, Any], str, str]:
    """YAML-шапка, тело ``.md`` и причина, если шапка не разобралась.

    Нет шапки вовсе (первая строка не ``---``) это не ошибка. Шапка без
    закрытия, с битым YAML или не словарём: шапка пустая, причина непуста.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text.strip(), ""
    end = next(
        (i for i in range(1, len(lines)) if lines[i].strip() == "---"), None
    )
    if end is None:
        return {}, text.strip(), "шапка не закрыта строкой ---"
    problem = ""
    try:
        meta = yaml.safe_load("".join(lines[1:end]))
    except yaml.YAMLError:
        meta, problem = None, "шапка не разобрана как YAML"
    if meta is not None and not isinstance(meta, dict):
        meta, problem = None, "шапка не словарь"
    body = "".join(lines[end + 1 :]).strip()
    return (meta or {}), body, problem


def skill_id_segment(skill_dir: Path) -> str:
    """Имя skill в id (``<плагин>:<имя>``) по правилу реестра skills:
    ``id`` из шапки ``SKILL.md``, иначе имя каталога."""
    text = _read_text(skill_dir / "SKILL.md") or ""
    meta_id = _split_markdown(text)[0].get("id")
    return (meta_id if isinstance(meta_id, str) else "") or skill_dir.name


def _names(value: Any) -> tuple[str, ...] | None:
    """Имена из строки через запятую или списка; ``None``, если тип другой."""
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, list):
        items = [v for v in value if isinstance(v, str)]
    else:
        return None
    return tuple(dict.fromkeys(i.strip() for i in items if i.strip()))


def _agent_tools(
    meta: dict[str, Any], header_problem: str
) -> tuple[tuple[str, ...] | None, str]:
    """Инструменты роли и причина. Закрыто на отказ: ключ есть (пусто,
    ``null``, не тот тип) или шапка битая, значит ``()``, а не «все»."""
    if header_problem:
        return (), header_problem
    if "tools" not in meta:
        return None, ""
    names = _names(meta["tools"])
    if names is None:
        value = meta["tools"]
        kind = "null" if value is None else type(value).__name__
        return (), f"tools: ожидались строка или список, получено {kind}"
    return names, ""


def _markdown_files(sources: tuple[Path, ...]) -> list[Path]:
    """Файлы ``*.md``: из каталогов (без вложенных, по имени) и сами файлы;
    один файл по одному разрешённому пути один раз."""
    found: dict[Path, None] = {}
    for src in sources:
        if src.is_dir():
            files = sorted(p.resolve() for p in src.glob("*.md"))
            found.update(dict.fromkeys(files))
        elif src.suffix == ".md":
            found[src.resolve()] = None
    return [p for p in found if p.is_file()]


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        logger.warning("plugin file %s unreadable: %s", path, exc)
        return None


def _load_commands(sources: tuple[Path, ...]) -> tuple[PluginCommand, ...]:
    out: list[PluginCommand] = []
    for path in _markdown_files(sources):
        text = _read_text(path)
        if text is None:
            continue
        meta, body, problem = _split_markdown(text)
        if problem:
            logger.warning("plugin command %s: %s", path, problem)
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


def _load_agents(sources: tuple[Path, ...]) -> tuple[PluginAgent, ...]:
    out: list[PluginAgent] = []
    for path in _markdown_files(sources):
        text = _read_text(path)
        if text is None:
            continue
        meta, body, header_problem = _split_markdown(text)
        if not body:
            logger.warning("plugin agent %s has no role text, skipped", path)
            continue
        tools, problem = _agent_tools(meta, header_problem)
        if problem:
            logger.warning("plugin agent %s: %s", path, problem)
        out.append(
            PluginAgent(
                name=str(meta.get("name") or path.stem).strip(),
                description=str(meta.get("description") or "").strip(),
                tools=tools,
                body=body,
                source=str(path),
                problem=problem,
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

    # Каталог данных создаёт ``mcp_servers.plugin_mcp_part`` при сборке
    # снимка, не здесь: чтение плагина (prepare, /plugins) ничего не пишет.
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


def _declared_paths(
    manifest: dict[str, Any], key: str, root: Path
) -> tuple[Path, ...]:
    """Пути компонента из манифеста Claude Code (строка или список).

    Каждый разрешается с раскрытием ссылок и обязан остаться внутри плагина;
    остальные отбрасываются с предупреждением. Повторы убираются.
    """
    raw = manifest.get(key)
    items = [raw] if isinstance(raw, str) else raw
    found: dict[Path, None] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, str) or not item.strip():
            continue
        path = (root / item).resolve()
        if path.is_relative_to(root):
            found[path] = None
        else:
            logger.warning(
                "plugin %s: %s path %r is outside the plugin, ignored",
                root,
                key,
                item,
            )
    return tuple(found)


def _component_sources(
    manifest: dict[str, Any], key: str, root: Path
) -> tuple[Path, ...]:
    """Стандартный каталог ``<key>/`` и пути из манифеста, без повторов."""
    standard = (root / key).resolve()
    declared = _declared_paths(manifest, key, root)
    return tuple(dict.fromkeys((standard, *declared)))


def _mcp_documents(manifest: dict[str, Any], root: Path) -> list[Any]:
    """Документы MCP Claude Code: стандартный файл, затем объявленные
    (``mcpServers`` в манифесте: путь, список путей или сам объект)."""
    standard = next(
        (root / rel for rel in _MCP_CANDIDATES if (root / rel).is_file()),
        None,
    )
    docs: list[Any] = []
    if standard is not None:
        docs.append(_read_json(standard))
    declared = manifest.get("mcpServers")
    if isinstance(declared, dict):
        docs.append(declared)
    taken = standard.resolve() if standard is not None else None
    for path in _declared_paths(manifest, "mcpServers", root):
        if path.is_file() and path != taken:
            docs.append(_read_json(path))
    return docs


def _load_mcp_servers(
    root: Path, fmt: str, name: str, manifest: dict[str, Any]
) -> McpParse:
    """Серверы MCP плагина: ``mcp.json`` для Agent Plugins; для Claude Code
    ``.mcp.json`` / ``mcp.json`` и серверы, объявленные в манифесте."""
    if fmt == PLUGIN_FORMAT_AP:
        path = root / _AP_MCP_FILE
        if not path.is_file():
            return {}, {}, []
        return _parse_ap_mcp_servers(_read_json(path), root, name)
    out: dict[str, dict[str, Any]] = {}
    shown: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for raw in _mcp_documents(manifest, root):
        got, got_shown, got_problems = _parse_mcp_servers(raw, root)
        for key, entry in got.items():
            out.setdefault(key, entry)
        for key, entry in got_shown.items():
            shown.setdefault(key, entry)
        problems.extend(got_problems)
    return out, shown, problems


_AP_IGNORED = (_HOOKS_FILE, "commands", "agents")


def _cc_components(
    root: Path, fmt: str, manifest: dict[str, Any]
) -> tuple[HookParse, tuple[PluginCommand, ...], tuple[PluginAgent, ...]]:
    """Хуки, команды и агенты; у Agent Plugins их нет (только skills и MCP)."""
    if fmt == PLUGIN_FORMAT_AP:
        ignored = [rel for rel in _AP_IGNORED if (root / rel).exists()]
        if ignored:
            logger.info(
                "plugin %s: Agent Plugins format, ignored: %s",
                root,
                ", ".join(ignored),
            )
        return ({}, ()), (), ()
    hooks_path = root / _HOOKS_FILE
    hooks = (
        _parse_hooks(_read_json(hooks_path))
        if hooks_path.is_file()
        else ({}, ())
    )
    return (
        hooks,
        _load_commands(_component_sources(manifest, "commands", root)),
        _load_agents(_component_sources(manifest, "agents", root)),
    )


def _skills_dirs(
    root: Path, fmt: str, manifest: dict[str, Any]
) -> tuple[Path, ...]:
    if fmt == PLUGIN_FORMAT_AP:
        sources = (root / "skills",)
    else:
        sources = _component_sources(manifest, "skills", root)
    return tuple(d for d in sources if d.is_dir())


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
    (hooks, hook_problems), commands, agents = _cc_components(
        root, fmt, manifest
    )
    name = str(manifest.get("name") or root.name)
    version = str(manifest.get("version") or "0")
    mcp_servers, mcp_shown, mcp_problems = _load_mcp_servers(
        root, fmt, name, manifest
    )
    return Plugin(
        name=name,
        version=version,
        root=root,
        skills_dirs=_skills_dirs(root, fmt, manifest),
        hooks=hooks,
        hook_problems=hook_problems,
        mcp_servers=mcp_servers,
        mcp_servers_raw=mcp_shown,
        mcp_problems=tuple(mcp_problems),
        commands=commands,
        agents=agents,
        format=fmt,
    )
