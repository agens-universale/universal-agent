"""GigaChat pre-call schema normalization + post-call plain-text rescue.

Pre-call: GigaChat's strict JSON Schema validator rejects several constructs
that LiteLLM otherwise forwards as-is. A 9-walker pipeline normalizes tool
parameter schemas before they reach the upstream:

1. ``_strip_strict_flag``       — drop non-standard ``"strict": true`` on tool spec
2. ``_resolve_refs``             — inline ``$ref``/``$defs`` (pydantic-emitted)
3. ``_flatten_anyof_oneof``      — pick first non-null variant from anyOf/oneOf
4. ``_normalize_nullable_types`` — drop ``null`` from array-form types
5. ``_ensure_object_properties`` — every ``"type": "object"`` gets ``properties``
6. ``_ensure_field_types``       — every leaf inside properties gets a ``type``
7. ``_normalize_additional_properties`` — coerce sub-schema → ``false``
8. ``_strip_unsupported_keywords`` — drop ``default``/``minimum``/``pattern``/…
9. ``_remap_reserved_tool_names`` — rename collisions with GigaChat built-ins

Post-call: GigaChat-2 models often return finalization as plain text in
``content`` instead of native ``tool_calls`` (four observed forms — see
``synthesize_tool_call`` docstring). A three-level rescue turns those
into native tool_calls before the agent sees them, avoiding an
``Undefined mode`` retry loop (~6-7k tokens/cycle on Max).
"""
from typing import Any

from callbacks._common import (
    detect_tool_call_from_payload,
    get_message_field,
    get_message_from_choice,
    iter_response_choices,
    make_tool_call,
    parse_json_object,
    set_choice_finish_reason,
    set_message_tool_call,
)


# Keywords inside JSON Schema that GigaChat's strict validator either rejects
# or silently mishandles. Stripped recursively from any schema node.
_UNSUPPORTED_SCHEMA_KEYWORDS = frozenset({
    "default",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "pattern",
    "format",
    "minItems",
    "maxItems",
    "uniqueItems",
})

# Built-in GigaChat function names that shadow user tools if reused.
# Sourced from https://developers.sber.ru/docs/ru/gigachat/api/function-calling
_RESERVED_FUNCTION_NAMES = frozenset({
    "text2image",
    "text2model3d",
    "get_file_content",
    "web_search",
})

_SUPERQUOTE_TOKEN = "<|superquote|>"


# ── Pre-call walkers ─────────────────────────────────────────────────────────

def _strip_strict_flag(tool: dict) -> bool:
    """Drop top-level ``"strict"`` from a function-tool spec.

    OAI Responses-flat layout in this repo carries ``"strict": true`` on every
    tool. GigaChat's API doesn't define this flag and forwarding it is
    unnecessary noise (some gateways reject unknown top-level keys).
    """
    fn = tool.get("function")
    changed = False
    if isinstance(fn, dict) and "strict" in fn:
        del fn["strict"]
        changed = True
    if "strict" in tool:
        del tool["strict"]
        changed = True
    return changed


def _ensure_object_properties(node: Any) -> None:
    """For every ``{"type": "object"}`` node, ensure ``properties`` exists."""
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" not in node:
            node["properties"] = {}
        for value in node.values():
            _ensure_object_properties(value)
    elif isinstance(node, list):
        for item in node:
            _ensure_object_properties(item)


def _ensure_field_types(node: Any) -> None:
    """For every leaf inside a ``properties`` map ensure ``type`` is set.

    Default for typeless fields is ``"object"`` with empty ``properties``
    (matches GigaChat's "any structured value" semantics).
    """
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            for field_schema in properties.values():
                if isinstance(field_schema, dict) and "type" not in field_schema:
                    field_schema["type"] = "object"
                    field_schema.setdefault("properties", {})
        for value in node.values():
            _ensure_field_types(value)
    elif isinstance(node, list):
        for item in node:
            _ensure_field_types(item)


def _normalize_additional_properties(node: Any) -> None:
    """Coerce ``additionalProperties: <sub-schema>`` to ``false``.

    GigaChat's validator only accepts boolean ``additionalProperties``.
    """
    if isinstance(node, dict):
        ap = node.get("additionalProperties")
        if ap is not None and not isinstance(ap, bool):
            node["additionalProperties"] = False
        for value in node.values():
            _normalize_additional_properties(value)
    elif isinstance(node, list):
        for item in node:
            _normalize_additional_properties(item)


def _strip_unsupported_keywords(node: Any) -> int:
    """Recursively remove keywords GigaChat doesn't validate cleanly."""
    removed = 0
    if isinstance(node, dict):
        for kw in list(node.keys()):
            if kw in _UNSUPPORTED_SCHEMA_KEYWORDS:
                del node[kw]
                removed += 1
        for value in node.values():
            removed += _strip_unsupported_keywords(value)
    elif isinstance(node, list):
        for item in node:
            removed += _strip_unsupported_keywords(item)
    return removed


def _resolve_refs(node: Any, defs: dict, depth: int = 0) -> Any:
    """Inline ``$ref`` pointers (``#/$defs/Name``) and strip ``$defs``.

    Ported from ai-forever/gpt2giga ``common/json_schema.py::resolve_schema_refs``.
    """
    if depth > 64:
        return node
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref.split("/", 2)[-1]
            target = defs.get(name)
            if isinstance(target, dict):
                resolved = _resolve_refs(target, defs, depth + 1)
                merged = dict(resolved) if isinstance(resolved, dict) else {}
                for k, v in node.items():
                    if k != "$ref" and k not in merged:
                        merged[k] = v
                return merged
        out: dict = {}
        for k, v in node.items():
            if k == "$defs":
                continue
            out[k] = _resolve_refs(v, defs, depth + 1)
        return out
    if isinstance(node, list):
        return [_resolve_refs(item, defs, depth + 1) for item in node]
    return node


def _normalize_nullable_types(node: Any) -> int:
    """``type: ["string", "null"]`` → ``type: "string"`` (drop null branch)."""
    patched = 0
    if isinstance(node, dict):
        t = node.get("type")
        if isinstance(t, list):
            non_null = [x for x in t if x != "null"]
            node["type"] = non_null[0] if non_null else "string"
            patched += 1
        for v in node.values():
            patched += _normalize_nullable_types(v)
    elif isinstance(node, list):
        for item in node:
            patched += _normalize_nullable_types(item)
    return patched


def _flatten_anyof_oneof(node: Any) -> int:
    """Replace ``anyOf``/``oneOf`` with the first non-null variant; merge metadata.

    Ported from ai-forever/gpt2giga. GigaChat doesn't support anyOf/oneOf
    in tool parameter schemas — pydantic emits them for ``Optional[T]``.
    """
    patched = 0
    if isinstance(node, dict):
        for key in ("anyOf", "oneOf"):
            variants = node.get(key)
            if isinstance(variants, list) and variants:
                non_null = [
                    v for v in variants
                    if isinstance(v, dict) and v.get("type") != "null"
                ]
                chosen = non_null[0] if non_null else (
                    variants[0] if isinstance(variants[0], dict) else {}
                )
                for k, v in chosen.items():
                    if k not in node:
                        node[k] = v
                node.pop(key, None)
                patched += 1
        for v in node.values():
            patched += _flatten_anyof_oneof(v)
    elif isinstance(node, list):
        for item in node:
            patched += _flatten_anyof_oneof(item)
    return patched


def _remap_reserved_tool_names(tools: list[dict]) -> dict[str, str]:
    """Rename tools whose name collides with a GigaChat built-in function.

    Returns a {original_name: remapped_name} dict. One-way rename — GigaChat
    doesn't reuse the name in tool_calls beyond what we pass.

    Ported from ai-forever/gpt2giga ``convert_tool_to_giga_functions``.
    """
    remap: dict[str, str] = {}
    for tool in tools:
        fn = tool.get("function")
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if isinstance(name, str) and name in _RESERVED_FUNCTION_NAMES:
            new_name = f"__user_{name}"
            fn["name"] = new_name
            remap[name] = new_name
    return remap


# ── Pre-call orchestrator ────────────────────────────────────────────────────

def normalize_tools(tools: list[dict]) -> dict:
    """Apply the full GigaChat normalization pipeline to a tools list.

    Returns a dict of counters for diagnostic logging.
    """
    stats = {
        "tools": len(tools),
        "strict_stripped": 0,
        "keywords_stripped": 0,
        "nullable_normalized": 0,
        "anyof_flattened": 0,
        "reserved_remapped": 0,
        "refs_resolved": 0,
    }
    stats["reserved_remapped"] = len(_remap_reserved_tool_names(tools))
    for tool in tools:
        if _strip_strict_flag(tool):
            stats["strict_stripped"] += 1
        params = (tool.get("function") or {}).get("parameters")
        if not isinstance(params, dict):
            continue
        # Inline $ref pointers before any other walk so subsequent steps
        # don't have to look through reference indirection.
        defs = params.get("$defs") or params.get("definitions") or {}
        if isinstance(defs, dict) and defs:
            resolved = _resolve_refs(params, defs)
            if isinstance(resolved, dict):
                tool["function"]["parameters"] = resolved
                params = resolved
                stats["refs_resolved"] += 1
        stats["anyof_flattened"] += _flatten_anyof_oneof(params)
        stats["nullable_normalized"] += _normalize_nullable_types(params)
        _ensure_object_properties(params)
        _ensure_field_types(params)
        _normalize_additional_properties(params)
        stats["keywords_stripped"] += _strip_unsupported_keywords(params)
    return stats


# ── Post-call: 3-level synth ─────────────────────────────────────────────────

def _find_tool_by_name(name: str, tools: list[dict]) -> dict | None:
    for tool in tools:
        fn = tool.get("function") or {}
        if fn.get("name") == name:
            return tool
    return None


def _replace_superquote_tokens(text: str) -> str:
    """Replace Sber's internal ``<|superquote|>`` token with double-quote.

    Form 4 of GigaChat plain-text responses uses this token in place of
    JSON string delimiters. After substitution the text is regular JSON.
    """
    return text.replace(_SUPERQUOTE_TOKEN, '"')


def synthesize_tool_call(response: Any, tools: list[dict]) -> dict:
    """Walk response.choices; turn GigaChat plain-text finalizations into
    native ``tool_calls`` envelopes. Returns counters of which level fired.

    Three levels (each tried in order, first hit wins for that choice):

    L1: Form 3 — parse JSON in content (optionally fenced) and match against
        declared tools.
    L2: Form 4 — replace ``<|superquote|>`` tokens, then retry L1.
    L3: Forms 1/2 (or anything left) — if ``submit_solution`` is declared,
        wrap content text into a ``submit_solution`` tool_call to finalize.

    Skipped if response already has native ``tool_calls``.
    """
    stats = {"l1_fenced_json": 0, "l2_superquote": 0, "l3_submit_wrap": 0, "skipped": 0}
    if not tools:
        return stats

    has_submit_solution = _find_tool_by_name("submit_solution", tools) is not None

    for choice in iter_response_choices(response):
        message = get_message_from_choice(choice)
        if message is None:
            continue

        if get_message_field(message, "tool_calls"):
            stats["skipped"] += 1
            continue

        content = get_message_field(message, "content")
        if not isinstance(content, str) or not content.strip():
            continue

        # ── L1: try parsing fenced/raw JSON object (Form 3) ───────────────
        payload = parse_json_object(content)
        if payload is not None:
            match = detect_tool_call_from_payload(payload, tools)
            if match is not None:
                name, args_str = match
                if set_message_tool_call(
                    message, make_tool_call(name, args_str, prefix="giga_l1")
                ):
                    set_choice_finish_reason(choice, "tool_calls")
                    stats["l1_fenced_json"] += 1
                    continue

        # ── L2: replace <|superquote|> → " and retry L1 (Form 4) ──────────
        if _SUPERQUOTE_TOKEN in content:
            cleaned = _replace_superquote_tokens(content)
            payload = parse_json_object(cleaned)
            if payload is not None:
                match = detect_tool_call_from_payload(payload, tools)
                if match is not None:
                    name, args_str = match
                    if set_message_tool_call(
                        message, make_tool_call(name, args_str, prefix="giga_l2")
                    ):
                        set_choice_finish_reason(choice, "tool_calls")
                        stats["l2_superquote"] += 1
                        continue

        # ── L3: bare plain text — wrap in submit_solution if declared ─────
        # Only fires when submit_solution is one of the declared tools (it
        # always is in our agent setup). Wrapping non-finalizer content
        # avoids the Undefined-mode retry loop and saves 2-3 backend calls.
        # NOTE: solution_type is hardcoded to "direct_answer" — strictly
        # correct only for read-only intents; for file-creation tasks the
        # agent expects "full_solution". Acceptable trade-off given the
        # alternative is the retry loop.
        if has_submit_solution:
            text = content.strip()
            args = {
                "handoff": text,
                "task_report": text,
                "solution_type": "direct_answer",
            }
            if set_message_tool_call(
                message,
                make_tool_call("submit_solution", args, prefix="giga_l3"),
            ):
                set_choice_finish_reason(choice, "tool_calls")
                stats["l3_submit_wrap"] += 1
                continue

    return stats
