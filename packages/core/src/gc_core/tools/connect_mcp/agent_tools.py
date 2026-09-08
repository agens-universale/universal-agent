"""
Подготовка MCP-инструментов в формате tools_data для агента:
{ tool_name: {"tool_fabric": ..., "spec": ..., "description": ...} }
"""
import copy
import logging
from typing import Any, Dict, Optional

from gc_core.loop_detector.constants import GENERIC_PROFILE, LOOP_WARN_KEY
from gc_core.tools.connect_mcp.api import acall_mcp_tool, call_mcp_tool
from gc_core.tools.connect_mcp.registry import ToolsCache
from gc_core.tools.schema_optional import apply_openai_strict_object
from gc_core.trace_id import trace_id_from_config, trace_header_name

logger = logging.getLogger(__name__)


class MCPToolWrapper:
    """
    Обёртка над MCP-инструментом с интерфейсом как у локальных тулов:
    __call__(**arguments) -> str | dict | list.
    """

    def __init__(
        self,
        full_tool_name: str,
        servers_cfg: Dict[str, Dict[str, Any]],
        tools_registry: Dict[str, Dict[str, Any]],
        timeout: float,
        default_use_retry: bool,
        *,
        os_backend: Any = None,
    ) -> None:
        self._full_tool_name = full_tool_name
        self._servers_cfg = servers_cfg
        self._tools_registry = tools_registry
        self._timeout = timeout
        self._default_use_retry = default_use_retry
        self._os_backend = os_backend

    async def acall(self, **arguments: Any) -> Any:
        """Быстрый путь для цикла агента: диспетчер зовёт его без потока.

        Форма из docs/async.md — тул с парой ``acall``/``__call__``.
        Для SDK-транспортов вызов идёт прямо на loop вызывающего; легаси
        (свои http/stdio на requests и subprocess) уходят в поток внутри
        ``acall_mcp_tool``, потому что прервать их всё равно нечем.
        """
        return await acall_mcp_tool(
            full_tool_name=self._full_tool_name,
            arguments=arguments,
            servers_cfg=self._servers_cfg,
            tools_registry=self._tools_registry,
            timeout=self._timeout,
            default_use_retry=self._default_use_retry,
            os_backend=self._os_backend,
        )



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
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref.rsplit("/", 1)[-1]
            target = defs.get(name)
            if isinstance(target, dict):
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
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == "$defs":
                continue
            out[key] = _resolve_schema_refs(value, defs, depth + 1, resolving)
        return out
    if isinstance(node, list):
        return [
            _resolve_schema_refs(item, defs, depth + 1, resolving) for item in node
        ]
    return node


def _preserve_referenced_defs(parameters: Dict[str, Any], source_defs: dict[str, Any]) -> None:
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
        parameters["$defs"] = {
            name: copy.deepcopy(source_defs[name])
            for name in sorted(needed)
            if name in source_defs
        }
    elif "$defs" in parameters:
        del parameters["$defs"]


def _inline_parameter_schema_refs(parameters: Dict[str, Any]) -> None:
    """Expand Pydantic/FastMCP ``$defs`` before property normalization."""
    defs = parameters.get("$defs")
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
    if "description" in schema and isinstance(schema["description"], str) and schema["description"].strip():
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


def _normalize_schema_recursive(
    schema: Any, param_name: str = "", *, openai_strict: bool = False
) -> Any:
    """
    Рекурсивная нормализация фрагмента JSON Schema.
    Поддерживает объекты, массивы и простые типы.

    openai_strict: применять OpenAI strict-mode (все поля -> required). Управляется
    флагом backend.openai_strict_tools_schema; при False сохраняем исходный `required`
    MCP-сервера (иначе не-strict модели вроде deepseek опускают "опциональные"
    поля -> WrongFunctionArgumentsException).
    """
    if not isinstance(schema, dict):
        return {
            "type": "string",
            "description": f"{param_name} parameter. Raw schema value: {schema!r}",
        }

    bare_ref = _bare_schema_ref(schema)
    if bare_ref is not None:
        return bare_ref

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

    # Schema-combinator constructs (anyOf / oneOf / allOf) define the type via
    # their branches; a top-level `type` is optional in JSON Schema and adding
    # one here breaks validation when it conflicts with the combinator.
    # Example: {"anyOf": [{"type": "integer"}, {"type": "null"}]} for `int | None`
    # would otherwise gain `type: "string"` here, making any value invalid
    # (validator AND-s `type` with `anyOf`).
    has_combinator = any(k in schema for k in ("anyOf", "oneOf", "allOf"))
    has_ref = isinstance(schema.get("$ref"), str)

    if "type" not in schema and not has_combinator and not has_ref:
        if "properties" in schema:
            schema["type"] = "object"
        elif "items" in schema:
            schema["type"] = "array"
        else:
            schema["type"] = "string"

    schema_type = schema.get("type")

    if schema_type == "object":
        props = schema.get("properties")
        if not isinstance(props, dict):
            props = {}
            schema["properties"] = props

        required = schema.get("required")
        if not isinstance(required, list):
            required = []
            schema["required"] = required

        for name, subschema in list(props.items()):
            normalized_subschema = _normalize_schema_recursive(
                subschema, param_name=name, openai_strict=openai_strict
            )
            if not isinstance(normalized_subschema, dict):
                normalized_subschema = {
                    "type": "string",
                    "description": f"{name} parameter.",
                }
            _ensure_param_description(normalized_subschema, name)
            props[name] = normalized_subschema

        if schema.get("additionalProperties") is not False:
            schema["additionalProperties"] = False

        if openai_strict:
            apply_openai_strict_object(schema)

    elif schema_type == "array":
        items = schema.get("items")
        if items is not None:
            schema["items"] = _normalize_schema_recursive(
                items, param_name=f"{param_name}_item", openai_strict=openai_strict
            )

    else:
        _ensure_param_description(schema, param_name or "value")

    return schema


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

    if not isinstance(spec.get("description"), str) or not spec["description"].strip():
        spec["description"] = "No description provided by MCP server."
    else:
        # Note: actual MCP tool execution output is normalized by tools.connect_mcp.api.call_mcp_tool
        # into a stable JSON envelope. Keep this short to avoid bloating tool descriptions.
        spec["description"] = spec["description"].strip()

    # Ensure the agent knows about the normalized return format for ALL MCP tools.
    # (MCP servers may return arbitrary shapes; we wrap them consistently.)
    spec["description"] += (
        " Return format: JSON string envelope with fields {tool, ok, mcp_server, mcp_tool, arguments, result, error}. "
        "Binary payloads are replaced with the string 'binary data'."
    )

    parameters = spec.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }

    if "type" not in parameters:
        parameters["type"] = "object"

    if parameters.get("type") != "object":
        parameters = {
            "type": "object",
            "properties": {
                "value": _normalize_schema_recursive(
                    parameters, param_name="value", openai_strict=openai_strict
                ),
            },
            "required": ["value"],
            "additionalProperties": False,
        }

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
        normalized = _normalize_schema_recursive(
            param_schema, param_name=name, openai_strict=openai_strict
        )
        if not isinstance(normalized, dict):
            normalized = {
                "type": "string",
                "description": f"{name} parameter.",
            }
        _ensure_param_description(normalized, name)
        props[name] = normalized

    if "additionalProperties" not in parameters:
        parameters["additionalProperties"] = False

    if openai_strict:
        apply_openai_strict_object(parameters)

    defs = parameters.get("$defs")
    if isinstance(defs, dict):
        for def_name, def_schema in list(defs.items()):
            if isinstance(def_schema, dict):
                defs[def_name] = _normalize_schema_recursive(
                    def_schema, param_name=def_name, openai_strict=openai_strict
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
        rest = full_name[len(prefix):]
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
    config_dict: Dict[str, Any],
) -> Dict[str, Any]:
    """Копия конфига MCP-серверов с внешним trace_id.

    ПЕРЕСБОРКА, не мутация, и это требование корректности, а не стиль: в
    серверном режиме config_dict — shallow copy процесс-глобального конфига, то
    есть ``config_dict["mcp-servers"]`` — тот же объект. Мутация пометила бы все
    последующие раны процесса, включая раны без trace, а раны идут в
    конкурентных потоках и писали бы друг другу чужие id.

    Отсутствие trace → исходный объект возвращается по identity, то есть
    «ничего не изменилось» буквально.

    Рассылка — строго opt-in: дефолт ``trace_header_enabled`` False совпадает со
    схемным (``config_modules/mcp.py``), иначе руками собранный конфиг вёл бы
    себя не так, как прошедший валидацию.
    """
    trace_id = trace_id_from_config(config_dict)
    if not trace_id:
        return servers_cfg
    header_name = trace_header_name(config_dict, "trace_id")
    out: Dict[str, Any] = {}
    for name, srv in servers_cfg.items():
        if not isinstance(srv, dict) or not srv.get("trace_header_enabled", False):
            out[name] = srv
            continue
        transport = str(srv.get("transport") or "").strip()
        if transport in _TRACE_HEADER_TRANSPORTS:
            out[name] = {
                **srv,
                "headers": {**(srv.get("headers") or {}), header_name: trace_id},
            }
        elif transport in _TRACE_ENV_TRANSPORTS:
            out[name] = {
                **srv,
                "env": {**(srv.get("env") or {}), _TRACE_ENV_VAR: trace_id},
            }
        else:
            out[name] = srv
    return out


def _prepare_mcp_tools_data(
    config_dict: Dict[str, Any],
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
    servers_cfg = config_dict.get("mcp-servers") or {}
    if not servers_cfg:
        return None
    # Единственная точка: этот же объект уходит и в ToolsCache (весь discovery),
    # и в замыкание каждого MCPToolWrapper (все вызовы тулов), и повторно сюда же
    # для сабагентов через build_tools_data(child_config).
    servers_cfg = _servers_cfg_with_trace(servers_cfg, config_dict)

    cfg: Dict[str, Any] = {
        "mcp-servers": servers_cfg,
        "mcp-meta": config_dict.get("mcp-meta") or {},
        "use_retry": bool(config_dict.get("use_retry", False)),
    }

    if tools_cache is None:
        # `os_backend` приехал с develop: через него говорят клиенты
        # `execute_on: os_backend`. У ЧУЖОГО кэша бэкенд уже свой — владелец
        # завёл его вместе с кэшем, и переопределять его здесь нельзя.
        tools_cache = ToolsCache(cfg, os_backend=os_backend)
    else:
        # Чужой кэш адресуется по СОДЕРЖИМОМУ mcp-servers (_make_cache_key), а
        # cfg здесь пересобран из config_dict — поэтому обновляем ссылку, чтобы
        # ключ считался от актуального набора серверов, а не от того, с которым
        # кэш когда-то создали.
        tools_cache.rebind(cfg)
    return tools_cache, servers_cfg


def _assemble_mcp_tools_data(
    mcp_registry: Dict[str, Any],
    status_map: Dict[str, Dict[str, Any]],
    config_dict: Dict[str, Any],
    servers_cfg: Dict[str, Any],
    os_backend: Any = None,
) -> tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Собрать tools_data из результата дискавери. Ни сети, ни ожидания.

    Вынесено, чтобы у сборки был ОДИН экземпляр на обе формы вызова:
    асинхронную и синхронную. Между ними отличается ровно одна строка — как
    дождаться дискавери, — и дублировать ради неё сотню строк фабрик и
    нормализации схем не за что.
    """
    timeout = (
        config_dict.get("mcp-meta", {}).get("request_timeout_sec")
        or 600
    )

    default_use_retry = bool(config_dict.get("use_retry", False))
    openai_strict = bool(config_dict.get("openai_strict_tools_schema", False))

    def make_fabric(full_tool_name: str):
        def fabric(**_kwargs: Any) -> MCPToolWrapper:
            return MCPToolWrapper(
                full_tool_name,
                servers_cfg,
                mcp_registry,
                timeout,
                default_use_retry=default_use_retry,
                os_backend=os_backend,
            )

        return fabric

    result: Dict[str, Dict[str, Any]] = {}
    for full_name, entry in mcp_registry.items():
        tool_entry = (
            entry.get("tool")
            if isinstance(entry, dict) and "tool" in entry
            else entry
        )
        if not isinstance(tool_entry, dict):
            continue

        normalized_spec = normalize_tool_spec_for_agent(
            tool_entry, openai_strict=openai_strict
        )

        metadata = entry.get("metadata") if isinstance(entry, dict) else None
        server_key = None
        if isinstance(metadata, dict):
            server_key = metadata.get("mcp_server_key")
        server_cfg = servers_cfg.get(server_key) if server_key else None
        loop_warn = _loop_warn_for_mcp_tool(
            server_cfg if isinstance(server_cfg, dict) else None,
            _mcp_base_tool_name(full_name, entry if isinstance(entry, dict) else {}),
        )

        tool_data: Dict[str, Any] = {
            "tool_fabric": make_fabric(full_name),
            "spec": normalized_spec,
            "description": normalized_spec.get("description", ""),
        }
        if loop_warn is not None:
            tool_data[LOOP_WARN_KEY] = loop_warn
        result[full_name] = tool_data
    return result, status_map


async def aget_mcp_tools_data_for_config(
    config_dict: Dict[str, Any],
    on_progress=None,
    *,
    tools_cache: Optional[ToolsCache] = None,
    os_backend: Any = None,
) -> tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """MCP-тулы в формате tools_data, без потока на дискавери.

    Основная реализация. Синхронное имя ``get_mcp_tools_data_for_config``
    сохранено и работает через портал — на нём сидят прямые вызовы и тесты.
    """
    prepared = _prepare_mcp_tools_data(
        config_dict, tools_cache=tools_cache, os_backend=os_backend
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
        mcp_registry, status_map, config_dict, servers_cfg, os_backend=os_backend
    )


def get_mcp_tools_data_for_config(
    config_dict: Dict[str, Any],
    on_progress=None,
    *,
    tools_cache: Optional[ToolsCache] = None,
    os_backend: Any = None,
) -> tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Синхронный двойник. Реализация одна — асинхронная.

    ``run_coro_blocking``, а НЕ портал: портал отказывается работать из-под
    работающего loop (SyncApiInsideEventLoopError), а сюда законно приходят
    вызывающие, у которых loop уже крутится. До переезда дискавери звалась
    через тот же хелпер — семантику сохраняем.
    """
    from gc_core.utils.aio_compat import run_coro_blocking

    return run_coro_blocking(
        aget_mcp_tools_data_for_config(
            config_dict, on_progress, tools_cache=tools_cache, os_backend=os_backend
        )
    )


__all__ = [
    "MCPToolWrapper",
    "aget_mcp_tools_data_for_config",
    "get_mcp_tools_data_for_config",
]
