"""
Подготовка MCP-инструментов в формате tools_data для агента:
{ tool_name: {"tool_fabric": ..., "spec": ..., "description": ...} }
"""

import copy
import logging
from typing import Any, Dict, Optional

from gc_core.config_modules.field_meta import plain_dump
from gc_core.execution.tool_schema import (
    apply_openai_strict_object,
    is_open_object,
)
from gc_core.loop_detector.constants import GENERIC_PROFILE, LOOP_WARN_KEY
from gc_core.security.permission_mode import CAT_MCP
from gc_core.settings import settings_of
from gc_core.tools.connect_mcp.api import (
    acall_mcp_tool,  # transport injection compatibility
)
from gc_core.tools.connect_mcp.registry import ToolsCache
from gc_core.tools.connect_mcp.tool_wrapper import MCPToolWrapper
from gc_core.trace_id import trace_header_name, trace_id_from_config

logger = logging.getLogger(__name__)

_DEFS_KEY = "$defs"


def _collect_schema_refs(node: Any) -> set[str]:
    """Collect ``#/$defs/...`` names still referenced anywhere in *node*."""
    refs: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            refs.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            refs |= _collect_schema_refs(value)
    elif isinstance(node, list):
        for item in node:
            refs |= _collect_schema_refs(item)
    return refs


def _inline_defs_ref(
    node: dict, defs: dict[str, Any], depth: int, resolving: frozenset[str]
) -> "dict | None":
    """Узел с ``$ref`` на известный ``$defs`` → раскрытая схема; иначе None."""
    ref = node.get("$ref")
    if not (isinstance(ref, str) and ref.startswith("#/$defs/")):
        return None
    name = ref.rsplit("/", 1)[-1]
    target = defs.get(name)
    if not isinstance(target, dict):
        return None
    if name in resolving:
        return {"$ref": ref}
    resolved = _resolve_schema_refs(
        target, defs, depth + 1, resolving | {name}
    )
    merged = dict(resolved) if isinstance(resolved, dict) else {}
    for key, value in node.items():
        if key != "$ref" and key not in merged:
            merged[key] = value
    return merged


def _resolve_schema_refs(
    node: Any,
    defs: dict[str, Any],
    depth: int = 0,
    resolving: frozenset[str] | None = None,
) -> Any:
    """Inline ``#/$defs/...`` refs (OpenAI strict rejects bare ``$ref`` property schemas)."""
    if resolving is None:
        resolving = frozenset()
    if depth > 64:
        return node
    if isinstance(node, dict):
        inlined = _inline_defs_ref(node, defs, depth, resolving)
        if inlined is not None:
            return inlined
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == _DEFS_KEY:
                continue
            out[key] = _resolve_schema_refs(value, defs, depth + 1, resolving)
        return out
    if isinstance(node, list):
        return [
            _resolve_schema_refs(item, defs, depth + 1, resolving)
            for item in node
        ]
    return node


def _preserve_referenced_defs(
    parameters: Dict[str, Any], source_defs: dict[str, Any]
) -> None:
    """Keep ``$defs`` entries still pointed at by ``$ref`` after partial inlining."""
    needed = _collect_schema_refs(parameters)
    while needed:
        expanded = set(needed)
        for name in list(needed):
            def_schema = source_defs.get(name)
            if isinstance(def_schema, dict):
                expanded |= _collect_schema_refs(def_schema)
        if expanded == needed:
            break
        needed = expanded
    if needed:
        parameters[_DEFS_KEY] = {
            name: copy.deepcopy(source_defs[name])
            for name in sorted(needed)
            if name in source_defs
        }
    elif _DEFS_KEY in parameters:
        del parameters[_DEFS_KEY]


def _inline_parameter_schema_refs(parameters: Dict[str, Any]) -> None:
    """Expand Pydantic/FastMCP ``$defs`` before property normalization."""
    defs = parameters.get(_DEFS_KEY)
    if not isinstance(defs, dict) or not defs:
        return
    source_defs = copy.deepcopy(defs)
    resolved = _resolve_schema_refs(parameters, source_defs)
    if isinstance(resolved, dict):
        parameters.clear()
        parameters.update(resolved)
    _preserve_referenced_defs(parameters, source_defs)


def _ensure_param_description(schema: Dict[str, Any], param_name: str) -> None:
    """
    Гарантировать наличие description у параметра.
    При отсутствии используем title или генерируем простое описание.
    """
    if (
        "description" in schema
        and isinstance(schema["description"], str)
        and schema["description"].strip()
    ):
        return

    title = schema.get("title")
    if isinstance(title, str) and title.strip():
        schema["description"] = title.strip()
    else:
        schema["description"] = f"{param_name} parameter."


def _bare_schema_ref(schema: dict[str, Any]) -> dict[str, str] | None:
    ref = schema.get("$ref")
    if isinstance(ref, str):
        return {"$ref": ref}
    return None


def _normalize_combinator_branches(
    schema: dict, param_name: str, *, openai_strict: bool
) -> None:
    for comb_key in ("anyOf", "oneOf", "allOf"):
        branches = schema.get(comb_key)
        if isinstance(branches, list):
            schema[comb_key] = [
                _normalize_schema_recursive(
                    branch, param_name=param_name, openai_strict=openai_strict
                )
                if isinstance(branch, dict)
                else branch
                for branch in branches
            ]


def _infer_missing_type(schema: dict) -> None:
    # Schema-combinator constructs (anyOf / oneOf / allOf) define the type via
    # their branches; a top-level `type` is optional in JSON Schema and adding
    # one here breaks validation when it conflicts with the combinator.
    # Example: {"anyOf": [{"type": "integer"}, {"type": "null"}]} for `int | None`
    # would otherwise gain `type: "string"` here, making any value invalid
    # (validator AND-s `type` with `anyOf`).
    has_combinator = any(k in schema for k in ("anyOf", "oneOf", "allOf"))
    has_ref = isinstance(schema.get("$ref"), str)
    if "type" in schema or has_combinator or has_ref:
        return
    if "properties" in schema or "additionalProperties" in schema:
        schema["type"] = "object"
    elif "items" in schema:
        schema["type"] = "array"
    else:
        schema["type"] = "string"


def _normalize_property(
    name: str, subschema: Any, *, openai_strict: bool
) -> dict:
    normalized = _normalize_schema_recursive(
        subschema, param_name=name, openai_strict=openai_strict
    )
    if not isinstance(normalized, dict):
        normalized = {
            "type": "string",
            "description": f"{name} parameter.",
        }
    _ensure_param_description(normalized, name)
    return normalized


def _normalize_object_schema(schema: dict, *, openai_strict: bool) -> None:
    # Объект без перечисленных ключей (`fields: dict`, `dict[str, str]`) —
    # свободный: по JSON Schema лишние ключи разрешены, если не запрещены явно.
    # Закрыть его — запретить все ключи. Оставляем открытым; strict для такого
    # инструмента снимает проекция на отправке (`has_open_object`).
    if not schema.get("properties") and "additionalProperties" not in schema:
        schema["additionalProperties"] = True
    if is_open_object(schema):
        # Пустые `properties: {}` / `required: []` смысла не меняют, но OpenAI
        # без strict читает такой объект как пустой и шлёт `{}` (проверено на
        # gpt-5.4-mini). Оставляем только `additionalProperties`.
        for key in ("properties", "required"):
            if not schema.get(key):
                schema.pop(key, None)
        return

    props = schema.get("properties")
    if not isinstance(props, dict):
        props = {}
        schema["properties"] = props

    required = schema.get("required")
    if not isinstance(required, list):
        required = []
        schema["required"] = required

    for name, subschema in list(props.items()):
        props[name] = _normalize_property(
            name, subschema, openai_strict=openai_strict
        )

    if schema.get("additionalProperties") is not False:
        schema["additionalProperties"] = False

    if openai_strict:
        apply_openai_strict_object(schema)


def _normalize_schema_recursive(
    schema: Any, param_name: str = "", *, openai_strict: bool = False
) -> Any:
    """
    Рекурсивная нормализация фрагмента JSON Schema.
    Поддерживает объекты, массивы и простые типы.

    openai_strict: развернуть required на всех полях (OpenAI strict). Сборка
    канона вызывает это с False: флаг накладывается проекцией на отправке, не
    здесь. При False сохраняем исходный `required` MCP-сервера.
    """
    if not isinstance(schema, dict):
        return {
            "type": "string",
            "description": f"{param_name} parameter. Raw schema value: {schema!r}",
        }

    bare_ref = _bare_schema_ref(schema)
    if bare_ref is not None:
        return bare_ref

    _normalize_combinator_branches(
        schema, param_name, openai_strict=openai_strict
    )
    _infer_missing_type(schema)

    schema_type = schema.get("type")

    if schema_type == "object":
        _normalize_object_schema(schema, openai_strict=openai_strict)

    elif schema_type == "array":
        items = schema.get("items")
        if items is not None:
            schema["items"] = _normalize_schema_recursive(
                items,
                param_name=f"{param_name}_item",
                openai_strict=openai_strict,
            )

    else:
        _ensure_param_description(schema, param_name or "value")

    return schema


def _agent_tool_description(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        description = "No description provided by MCP server."
    else:
        # Note: actual MCP tool execution output is normalized by tools.connect_mcp.api.call_mcp_tool
        # into a stable JSON envelope. Keep this short to avoid bloating tool descriptions.
        description = raw.strip()
    # Ensure the agent knows about the normalized return format for ALL MCP tools.
    # (MCP servers may return arbitrary shapes; we wrap them consistently.)
    return description + (
        " Return format: JSON string envelope with fields {tool, ok, mcp_server, mcp_tool, arguments, result, error}. "
        "Binary payloads are replaced with the string 'binary data'."
    )


def _object_parameters(
    parameters: Any, *, openai_strict: bool
) -> Dict[str, Any]:
    """parameters как object-схема: отсутствующие → пустой объект, не-object → обёртка {value}."""
    if not isinstance(parameters, dict):
        return {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }
    if "type" not in parameters:
        parameters["type"] = "object"
    if parameters.get("type") != "object":
        return {
            "type": "object",
            "properties": {
                "value": _normalize_schema_recursive(
                    parameters, param_name="value", openai_strict=openai_strict
                ),
            },
            "required": ["value"],
            "additionalProperties": False,
        }
    return parameters


def normalize_tool_spec_for_agent(
    tool_spec: Dict[str, Any], *, openai_strict: bool = False
) -> Dict[str, Any]:
    """
    Привести произвольную сигнатуру MCP-инструмента к стандарту агента.
    Гарантируются:
    - type: "function"
    - непустой description
    - parameters.type == "object"
    - для всех parameters.properties.* есть description и адекватный type
    """
    spec = copy.deepcopy(tool_spec) if isinstance(tool_spec, dict) else {}

    if spec.get("type") != "function":
        spec["type"] = "function"

    spec["description"] = _agent_tool_description(spec.get("description"))

    parameters = _object_parameters(
        spec.get("parameters"), openai_strict=openai_strict
    )
    _inline_parameter_schema_refs(parameters)

    props = parameters.get("properties")
    if not isinstance(props, dict):
        props = {}
        parameters["properties"] = props

    required = parameters.get("required")
    if not isinstance(required, list):
        required = []
        parameters["required"] = required

    for name, param_schema in list(props.items()):
        props[name] = _normalize_property(
            name, param_schema, openai_strict=openai_strict
        )

    if "additionalProperties" not in parameters:
        parameters["additionalProperties"] = False

    if openai_strict:
        apply_openai_strict_object(parameters)

    defs = parameters.get(_DEFS_KEY)
    if isinstance(defs, dict):
        for def_name, def_schema in list(defs.items()):
            if isinstance(def_schema, dict):
                defs[def_name] = _normalize_schema_recursive(
                    def_schema,
                    param_name=def_name,
                    openai_strict=openai_strict,
                )

    spec["parameters"] = parameters
    spec.setdefault("strict", openai_strict)

    return spec


def _mcp_base_tool_name(full_name: str, entry: Dict[str, Any]) -> str:
    """Базовое имя тула как в tools/list, без префикса mcp_<server>_."""
    metadata = entry.get("metadata") if isinstance(entry, dict) else None
    if isinstance(metadata, dict):
        raw = metadata.get("mcp_tool_name")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    prefix = "mcp_"
    if full_name.startswith(prefix):
        rest = full_name[len(prefix) :]
        _, sep, tool = rest.partition("_")
        if sep and tool:
            return tool
    return full_name


def _loop_warn_for_mcp_tool(
    server_cfg: Dict[str, Any] | None,
    base_tool_name: str,
) -> Dict[str, int] | None:
    """Opt-in: generic-профиль только для явно указанных тулов сервера."""
    if not isinstance(server_cfg, dict):
        return None
    raw = server_cfg.get("loop_control_tools")
    if raw == "all":
        return dict(GENERIC_PROFILE)
    if not isinstance(raw, list):
        return None
    allowed = {str(name).strip() for name in raw if str(name).strip()}
    if base_tool_name in allowed:
        return dict(GENERIC_PROFILE)
    return None


# Транспорты, у которых есть канал HTTP-заголовков.
_TRACE_HEADER_TRANSPORTS = {"http", "sse", "stream_http", "stream_sse"}
# Транспорты без заголовков вообще: _request_stdio принимает аргумент headers и
# никогда его не использует, у SDK-пути stdio параметра headers нет. Единственный
# канал — переменные окружения дочернего процесса на спавне.
_TRACE_ENV_TRANSPORTS = {"stdio", "stream_stdio"}
# Имя переменной окружения для stdio-серверов.
_TRACE_ENV_VAR = "TRACE_ID"


def _servers_cfg_with_trace(
    servers_cfg: Dict[str, Any],
    settings: Any,
) -> Dict[str, Any]:
    """Копия конфига MCP-серверов с внешним trace_id.

    ПЕРЕСБОРКА, не мутация, и это требование корректности, а не стиль: в
    серверном режиме словари серверов могут делиться между ранами (кэш, общий
    конфиг процесса). Мутация пометила бы все последующие раны процесса,
    включая раны без trace, а раны идут в конкурентных потоках и писали бы
    друг другу чужие id.

    Отсутствие trace → исходный объект возвращается по identity, то есть
    «ничего не изменилось» буквально.

    Рассылка — строго opt-in: дефолт ``trace_header_enabled`` False совпадает со
    схемным (``config_modules/mcp.py``), иначе руками собранный конфиг вёл бы
    себя не так, как прошедший валидацию.
    """
    trace_id = trace_id_from_config(settings)
    if not trace_id:
        return servers_cfg
    header_name = trace_header_name(settings, "trace_id")
    out: Dict[str, Any] = {}
    for name, srv in servers_cfg.items():
        if not isinstance(srv, dict) or not srv.get(
            "trace_header_enabled", False
        ):
            out[name] = srv
            continue
        transport = str(srv.get("transport") or "").strip()
        if transport in _TRACE_HEADER_TRANSPORTS:
            out[name] = {
                **srv,
                "headers": {
                    **(srv.get("headers") or {}),
                    header_name: trace_id,
                },
            }
        elif transport in _TRACE_ENV_TRANSPORTS:
            out[name] = {
                **srv,
                "env": {**(srv.get("env") or {}), _TRACE_ENV_VAR: trace_id},
            }
        else:
            out[name] = srv
    return out


def mcp_cache_config(settings: Any) -> Dict[str, Any]:
    """Конфиг ``ToolsCache`` из настроек: секция ``mcp`` в проекции нижнего слоя.

    Ниже по слою (кэш, транспорты, ssl_config) конфиг сервера — словарь в
    проекции плоского вида: ключи по alias (`server-name`, `verify-ssl`, ...),
    секреты (token) — сырыми: транспорту нужен сам токен. Единственная точка:
    этот же объект уходит и в ToolsCache (весь discovery), и в замыкание
    каждого MCPToolWrapper (все вызовы тулов), и повторно сюда же для
    сабагентов через build_tools_data(настройки ребёнка).
    """
    app = settings_of(settings)
    servers_cfg: Dict[str, Any] = {
        name: plain_dump(server, by_alias=True)
        for name, server in app.mcp.servers.items()
    }
    return {
        "mcp-servers": _servers_cfg_with_trace(servers_cfg, settings),
        "mcp-meta": app.mcp.meta.model_dump(by_alias=True),
        "use_retry": app.backend.use_retry,
    }


def _prepare_mcp_tools_data(
    settings: Any,
    *,
    tools_cache: Optional[ToolsCache] = None,
    os_backend: Any = None,
):
    """Подготовить кэш и конфиг серверов перед дискавери.

    Отдаёт ``(tools_cache, servers_cfg)`` либо ``None``, если MCP не настроен.
    Вынесено, чтобы асинхронная и синхронная точки входа делили ОДНУ подготовку:
    отличаются они ровно тем, как дождаться дискавери.

    ``tools_cache`` — кэш, которым владеет ВЫЗЫВАЮЩИЙ. Не передан — заводится
    свой, одноразовый: ровно прежнее поведение, каждая сборка платит полную
    дискавери (866–1405 мс на реальном конфиге). Передан — попадания считаются
    по ``mcp-servers`` и TTL, и повторные сборки уже бесплатны.

    Кэш прокидывается, а не сделан процессным синглтоном, намеренно. Такой
    синглтон мы только что лечили в другом месте: ``PersistentStdioRegistry``
    был общим на процесс, и первый доигравший агент закрывал сессии остальных.
    У глобального кэша те же три беды — скрытое время жизни (никто не владеет,
    некому чистить), протечка между арендаторами сервера с разными наборами
    серверов, и переживание между тестами.
    """
    cfg = mcp_cache_config(settings)
    if not cfg["mcp-servers"]:
        return None
    servers_cfg = cfg["mcp-servers"]

    if tools_cache is None:
        # `os_backend` приехал с develop: через него говорят клиенты
        # `execute_on: os_backend`. У ЧУЖОГО кэша бэкенд уже свой — владелец
        # завёл его вместе с кэшем, и переопределять его здесь нельзя.
        tools_cache = ToolsCache(cfg, os_backend=os_backend)
    else:
        # Чужой кэш адресуется по СОДЕРЖИМОМУ mcp-servers (_make_cache_key), а
        # cfg здесь пересобран из настроек — поэтому обновляем ссылку, чтобы
        # ключ считался от актуального набора серверов, а не от того, с которым
        # кэш когда-то создали.
        _warn_if_cache_misses_the_sandbox(tools_cache, servers_cfg, os_backend)
        tools_cache.rebind(cfg)
    return tools_cache, servers_cfg


def _warn_if_cache_misses_the_sandbox(
    tools_cache: ToolsCache, servers_cfg: Dict[str, Any], os_backend: Any
) -> None:
    """Сказать вслух, что дискавери уйдёт мимо песочницы.

    Бэкенд кэш получает только при создании: `rebind` перепривязывает конфиг,
    а бэкенд принять ему нечем. Кэш без бэкенда считает `execute_on: os_backend`
    «своим», и для stdio это запуск команды сервера в процессе агента — мимо
    песочницы, тогда как вызовы тулов уходят в неё. Расхождение молчит: внизу,
    в `resolve_mcp_client`, об этом сказано в debug.

    Доложить можно только ЗДЕСЬ: лишь в этой точке видно обе стороны — что
    песочница у нас есть и что у кэша её нет. Ниже, где выбирается клиент,
    отсутствие бэкенда неотличимо от честного запуска без песочницы (CLI на
    хосте), и предупреждение сыпалось бы там на каждый вызов тула.

    Чинить за владельца нельзя: бэкенд — его выбор и его время жизни.
    """
    if not getattr(os_backend, "is_sandbox", False):
        return
    if getattr(tools_cache, "os_backend", None) is not None:
        return
    orphaned = sorted(
        name
        for name, server_cfg in (servers_cfg or {}).items()
        if isinstance(server_cfg, dict)
        and str(server_cfg.get("execute_on") or "agent") == "os_backend"
    )
    if not orphaned:
        return
    logger.warning(
        "ToolsCache was created without os_backend, so discovery for %s "
        "(execute_on: os_backend) runs in the agent process instead of the "
        "sandbox — for stdio that starts the server's command on the host. "
        "Pass the backend when creating the cache: ToolsCache(cfg, os_backend=…)",
        ", ".join(orphaned),
    )


def _split_registry_entry(entry: Any) -> tuple:
    """Запись реестра → (tool, metadata): {"tool", "metadata"} либо сам entry без metadata."""
    if isinstance(entry, dict) and "tool" in entry:
        return entry.get("tool"), entry.get("metadata")
    return entry, (entry.get("metadata") if isinstance(entry, dict) else None)


def _assemble_mcp_tools_data(
    mcp_registry: Dict[str, Any],
    status_map: Dict[str, Dict[str, Any]],
    settings: Any,
    servers_cfg: Dict[str, Any],
    os_backend: Any = None,
) -> tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Собрать tools_data из результата дискавери. Ни сети, ни ожидания.

    Вынесено, чтобы у сборки был ОДИН экземпляр на обе формы вызова:
    асинхронную и синхронную. Между ними отличается ровно одна строка — как
    дождаться дискавери, — и дублировать ради неё сотню строк фабрик и
    нормализации схем не за что.
    """
    app = settings_of(settings)
    timeout = app.mcp.meta.request_timeout_sec or 600

    default_use_retry = app.backend.use_retry
    # Канон MCP без флагов схемы: openai_strict/send_tool_strict накладываются
    # копией на отправке, не здесь.

    def make_fabric(full_tool_name: str):
        def fabric(**_kwargs: Any) -> MCPToolWrapper:
            return MCPToolWrapper(
                full_tool_name,
                servers_cfg,
                mcp_registry,
                timeout,
                default_use_retry=default_use_retry,
                os_backend=os_backend,
                caller=acall_mcp_tool,
            )

        return fabric

    result: Dict[str, Dict[str, Any]] = {}
    for full_name, entry in mcp_registry.items():
        tool_entry, metadata = _split_registry_entry(entry)
        if not isinstance(tool_entry, dict):
            continue

        normalized_spec = normalize_tool_spec_for_agent(tool_entry)
        meta = metadata if isinstance(metadata, dict) else {}
        server_key = meta.get("mcp_server_key")
        server_cfg = servers_cfg.get(server_key) if server_key else None
        base_name = _mcp_base_tool_name(
            full_name, entry if isinstance(entry, dict) else {}
        )
        loop_warn = _loop_warn_for_mcp_tool(
            server_cfg if isinstance(server_cfg, dict) else None,
            base_name,
        )

        tool_data: Dict[str, Any] = {
            "tool_fabric": make_fabric(full_name),
            "execution_kind": "mcp",
            # Режим разрешений: зона и сервер — по ним строится ключ решения.
            "category": CAT_MCP,
            "mcp_server": server_key,
            "mcp_tool": base_name,
            "mcp_title": meta.get("mcp_tool_title"),
            "spec": normalized_spec,
            "description": normalized_spec.get("description", ""),
        }
        if loop_warn is not None:
            tool_data[LOOP_WARN_KEY] = loop_warn
        result[full_name] = tool_data
    return result, status_map


async def aget_mcp_tools_data_for_config(
    settings: Any,
    on_progress=None,
    *,
    tools_cache: Optional[ToolsCache] = None,
    os_backend: Any = None,
) -> tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """MCP-тулы в формате tools_data, без потока на дискавери.

    Форма одна. Синхронного имени нет: единственный вызывающий — сборка
    набора тулов, и она корутинная.
    """
    prepared = _prepare_mcp_tools_data(
        settings, tools_cache=tools_cache, os_backend=os_backend
    )
    if prepared is None:
        return {}, {}
    cache, servers_cfg = prepared
    try:
        mcp_registry = await cache.aget_tools(on_progress=on_progress)
        status_map = cache.get_status()
    except Exception:
        logger.error("Failed to load MCP tools", exc_info=True)
        return {}, {}
    return _assemble_mcp_tools_data(
        mcp_registry,
        status_map,
        settings,
        servers_cfg,
        os_backend=os_backend,
    )


__all__ = [
    "MCPToolWrapper",
    "aget_mcp_tools_data_for_config",
]
