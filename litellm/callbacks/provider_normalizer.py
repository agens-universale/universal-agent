"""LiteLLM Proxy pre/post-call hooks: normalize requests and responses for
strict / partially-OAI-compatible providers.

Idea (mirror of OpenHands' approach where provider quirks live in the LLM
layer, not in agent code): keep the agent payload pure OpenAI Chat Completions
shape; mutate only inside the proxy, per provider, right before the upstream
HTTP call (pre-call) or right after (post-call).

Currently handles:

1) GigaChat — strict JSON Schema validator (pre-call). Requires:
   - every ``{"type": "object"}`` carries ``"properties"`` (empty ``{}`` is fine);
   - every leaf inside ``properties`` has a ``"type"``;
   - ``additionalProperties`` is a boolean (not a sub-schema);
   - unsupported keywords stripped from schemas: ``default``, ``minimum``,
     ``maximum``, ``pattern``, ``format``, ``minLength``, ``maxLength``,
     ``exclusiveMinimum``, ``exclusiveMaximum``, ``multipleOf``;
   - top-level ``"strict"`` flag on the function spec is non-standard and
     stripped (LiteLLM forwards it as-is otherwise).

2) Yandex Cloud (OpenAI-compatible endpoint) — two issues, both handled here:
   - (pre-call) rejects messages where ``content`` is ``""`` / ``None`` /
     ``[]`` with HTTP 400 ``empty message text``;
   - (post-call) does NOT have a native LiteLLM provider, so multi-turn tool
     calling breaks: Yandex returns the chosen tool's arguments as a JSON
     string inside ``message.content`` (often wrapped in ```` ```...``` ````
     fences), instead of the OpenAI ``tool_calls`` array. LiteLLM's generic
     ``openai/`` provider passes this through unparsed, and the agent sees
     ``content`` text rather than a tool call. We synthesize the missing
     ``tool_calls`` array post-call by matching parsed JSON keys against
     declared tool schemas (request-side ``tools`` array).

Wire-up (litellm/proxy_config.yaml):

    litellm_settings:
      callbacks: callbacks.provider_normalizer.proxy_handler_instance

The proxy must be started from inside ``litellm/`` (so that ``callbacks``
is importable as a top-level package). ``litellm/run.py`` runs the
subprocess with ``cwd=litellm/`` to satisfy this.
"""

import json
import logging
import re
import uuid
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger(__name__)


# Keywords inside JSON Schema that GigaChat's strict validator either rejects
# or silently mishandles. Stripped recursively from any schema node.
_GIGACHAT_UNSUPPORTED_SCHEMA_KEYWORDS = frozenset({
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


def _ensure_object_properties(node: Any) -> None:
    """For every ``{"type": "object"}`` node, ensure ``properties`` exists.

    No-op for nodes already carrying ``properties``. Walks dicts and lists.
    """
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
    (matches GigaChat's "any structured value" semantics — fits cases like
    ``submit_solution.handoff`` which is intentionally tool-agnostic).
    Walks recursively to catch nested ``properties``.
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
    A sub-schema (used in our repo to constrain map values, e.g. ``env`` in
    ``execute_command``/``run_python_code``) is rejected. Coercing to ``false``
    is the safe default — it locks the map to declared keys only, which for
    our cases (empty ``properties: {}``) effectively allows any unstructured
    object via the upstream's permissive parsing.
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
    """Recursively remove keywords GigaChat doesn't validate cleanly.

    Returns the number of keywords removed (for logging).
    """
    removed = 0
    if isinstance(node, dict):
        for kw in list(node.keys()):
            if kw in _GIGACHAT_UNSUPPORTED_SCHEMA_KEYWORDS:
                del node[kw]
                removed += 1
        for value in node.values():
            removed += _strip_unsupported_keywords(value)
    elif isinstance(node, list):
        for item in node:
            removed += _strip_unsupported_keywords(item)
    return removed


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


def _backfill_empty_content(messages: list[dict]) -> int:
    """Ensure every message has non-empty ``content``.

    Yandex Cloud's OAI-compat endpoint rejects with HTTP 400
    ``empty message text`` if a message has any of:
      - ``content`` key absent (assistant-only-tool_calls case)
      - ``content`` is ``None`` / ``""`` / ``[]``
      - ``content`` is a list whose text parts have empty ``text``

    All these are valid OpenAI shapes; we coerce to a single space, which
    keeps semantics for the model but satisfies Yandex's validator.
    Returns the number of messages that were patched (for logging).
    """
    patched = 0
    for msg in messages:
        c = msg.get("content")
        if c is None or c == "" or c == []:
            msg["content"] = " "
            patched += 1
            continue
        if isinstance(c, list):
            for part in c:
                if (
                    isinstance(part, dict)
                    and part.get("type") == "text"
                    and not part.get("text")
                ):
                    part["text"] = " "
                    patched += 1
    return patched


# ── Yandex post-call: synthesize tool_calls from JSON-in-content ────────────

_MARKDOWN_FENCE_RE = re.compile(
    r"^```(?:json|JSON)?\s*\n(.*?)\n```\s*$", re.DOTALL,
)


def _strip_markdown_fence(text: str) -> str:
    """Strip ``` ```json ... ``` ``` fence around a JSON object, if present."""
    s = text.strip()
    m = _MARKDOWN_FENCE_RE.match(s)
    if m:
        return m.group(1).strip()
    return s


def _parse_json_object(text: str) -> dict | None:
    """Parse ``text`` as a JSON object. Return dict or None."""
    s = _strip_markdown_fence(text)
    if not (s.startswith("{") and s.endswith("}")):
        return None
    try:
        parsed = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _detect_tool_call_from_payload(
    payload: dict, tools: list[dict]
) -> tuple[str, str] | None:
    """Match ``payload`` keys against declared ``tools`` to identify which
    tool the model intended to call.

    Two shapes accepted:
      1. Native OpenAI tool_call shape: ``{"name": "<tool>", "arguments": {...}}``.
         Use directly if name matches a declared tool.
      2. Flat argument payload: ``{"path": "/tmp/x"}``. Match by required+overlap.

    Returns ``(tool_name, arguments_json_str)`` or None.
    """
    # Shape 1: native tool_call envelope.
    if (
        isinstance(payload.get("name"), str)
        and "arguments" in payload
    ):
        name = payload["name"]
        args = payload["arguments"]
        for tool in tools:
            fn = tool.get("function") or {}
            if fn.get("name") == name:
                if isinstance(args, dict):
                    args_str = json.dumps(args, ensure_ascii=False)
                elif isinstance(args, str):
                    args_str = args
                else:
                    args_str = json.dumps(args, ensure_ascii=False)
                return name, args_str

    payload_keys = set(payload.keys())
    best_name: str | None = None
    best_score = -1
    for tool in tools:
        fn = tool.get("function") or {}
        name = fn.get("name")
        if not isinstance(name, str):
            continue
        params = fn.get("parameters") or {}
        required = set(params.get("required") or [])
        properties = set((params.get("properties") or {}).keys())
        if required and not required.issubset(payload_keys):
            continue
        # Score: count of payload keys that exist in this tool's properties.
        # Tools with empty properties (e.g. submit_solution before normalization)
        # won't match — that's fine, they wouldn't have been called anyway.
        score = len(payload_keys & properties)
        if score > best_score:
            best_score = score
            best_name = name
    if best_name is None or best_score == 0:
        return None
    args_json = json.dumps(payload, ensure_ascii=False)
    return best_name, args_json


def _synthesize_tool_calls_for_yandex(response: Any, tools: list[dict]) -> int:
    """Walk response choices; for any assistant message that has plain text
    ``content`` matching a known tool's argument schema, synthesize the
    ``tool_calls`` array and clear ``content``.

    Returns the number of choices patched.
    """
    if not tools:
        return 0
    # ModelResponse / dict tolerance.
    choices = getattr(response, "choices", None)
    if choices is None and isinstance(response, dict):
        choices = response.get("choices")
    if not choices:
        return 0

    patched = 0
    for choice in choices:
        message = getattr(choice, "message", None)
        if message is None and isinstance(choice, dict):
            message = choice.get("message")
        if message is None:
            continue

        # Skip if real tool_calls already present.
        existing_tool_calls = (
            getattr(message, "tool_calls", None)
            if not isinstance(message, dict)
            else message.get("tool_calls")
        )
        if existing_tool_calls:
            continue

        content = (
            getattr(message, "content", None)
            if not isinstance(message, dict)
            else message.get("content")
        )
        if not isinstance(content, str) or not content.strip():
            continue

        payload = _parse_json_object(content)
        if payload is None:
            continue

        match = _detect_tool_call_from_payload(payload, tools)
        if match is None:
            continue
        tool_name, args_str = match

        synthetic_call = {
            "id": f"call_yandex_{uuid.uuid4().hex[:16]}",
            "type": "function",
            "function": {"name": tool_name, "arguments": args_str},
        }

        if isinstance(message, dict):
            message["tool_calls"] = [synthetic_call]
            message["content"] = None
        else:
            try:
                message.tool_calls = [synthetic_call]  # type: ignore[attr-defined]
                message.content = None  # type: ignore[attr-defined]
            except (AttributeError, TypeError):
                # Fallback to dict-style write if attribute set is rejected.
                try:
                    message["tool_calls"] = [synthetic_call]  # type: ignore[index]
                    message["content"] = None  # type: ignore[index]
                except Exception:
                    continue

        # Set finish_reason to tool_calls so downstream agent knows.
        for finish_target in (choice, getattr(choice, "_choice", None)):
            if finish_target is None:
                continue
            try:
                if isinstance(finish_target, dict):
                    finish_target["finish_reason"] = "tool_calls"
                else:
                    finish_target.finish_reason = "tool_calls"  # type: ignore[attr-defined]
                break
            except (AttributeError, TypeError):
                continue

        patched += 1
    return patched


# ── GigaChat post-call: synthesize tool_calls from plain text / fenced JSON ──
#
# GigaChat-2 models often return finalization as plain text in ``content``
# instead of native ``tool_calls``. Four observed forms (Lite/Pro/Max all):
#
#   Form 1 — bare plain text: ``content: "623"``, no tool_calls.
#   Form 2 — explanatory text: ``content: "Произведение чисел 89 и 7 равно 623."``.
#   Form 3 — markdown-fenced JSON wrapper:
#            ``content: "```json\n{\"function\": \"calculator\", \"arguments\": {...}}\n```"``
#   Form 4 — Sber-internal ``<|superquote|>`` tokens (instead of double quotes):
#            ``content: "{<|superquote|>function name<|superquote|>: <|superquote|>calculator<|superquote|>, ...}"``
#
# Without a post-call hook the agent sees ``finish_reason: "stop"`` + plain text,
# falls into ``Undefined mode`` recovery loop, and retries via technical user
# feedback — which GigaChat models also fail to react to (3-4 retry cycles on
# Max, never on Lite/Pro). Cost on Max: ~27k tokens for a single calc task.
#
# This hook tries three levels (cheapest first):
#   1. Parse Form 3 (markdown-fenced JSON) — direct tool_call synthesis.
#   2. Replace ``<|superquote|>`` → ``"`` then try Form 3 again.
#   3. If declared tools contain ``submit_solution``, wrap any remaining
#      plain text into a ``submit_solution(handoff=<content>, task_report=...)``
#      tool_call (gives the agent a finalize signal instead of looping).
#
# Mirrors ``_synthesize_tool_calls_for_yandex`` design.

_SUPERQUOTE_TOKEN = "<|superquote|>"


def _replace_superquote_tokens(text: str) -> str:
    """Replace Sber's internal ``<|superquote|>`` token with double-quote.

    Form 4 of GigaChat plain-text responses uses this token in place of
    JSON string delimiters. After substitution the text is regular JSON
    and can be parsed by ``_parse_json_object``.
    """
    return text.replace(_SUPERQUOTE_TOKEN, '"')


def _find_tool_by_name(name: str, tools: list[dict]) -> dict | None:
    for tool in tools:
        fn = tool.get("function") or {}
        if fn.get("name") == name:
            return tool
    return None


def _make_tool_call(name: str, arguments: Any, prefix: str = "giga") -> dict:
    """Build an OpenAI-shape tool_call envelope.

    ``arguments`` may be a dict (will be JSON-stringified) or a string
    (passed through).
    """
    if isinstance(arguments, dict):
        args_str = json.dumps(arguments, ensure_ascii=False)
    elif isinstance(arguments, str):
        args_str = arguments
    else:
        args_str = json.dumps(arguments, ensure_ascii=False)
    return {
        "id": f"call_{prefix}_{uuid.uuid4().hex[:16]}",
        "type": "function",
        "function": {"name": name, "arguments": args_str},
    }


def _set_message_tool_call(message: Any, tool_call: dict) -> bool:
    """Mutate ``message`` to carry a single ``tool_calls`` entry and clear content.

    Tolerant to both pydantic-model and dict representations of message.
    Returns True on success, False on failure (in which case caller skips).
    """
    if isinstance(message, dict):
        message["tool_calls"] = [tool_call]
        message["content"] = None
        return True
    try:
        message.tool_calls = [tool_call]  # type: ignore[attr-defined]
        message.content = None  # type: ignore[attr-defined]
        return True
    except (AttributeError, TypeError):
        try:
            message["tool_calls"] = [tool_call]  # type: ignore[index]
            message["content"] = None  # type: ignore[index]
            return True
        except Exception:
            return False


def _set_choice_finish_reason(choice: Any, reason: str) -> None:
    """Set ``finish_reason: "tool_calls"`` on choice (pydantic-or-dict)."""
    for target in (choice, getattr(choice, "_choice", None)):
        if target is None:
            continue
        try:
            if isinstance(target, dict):
                target["finish_reason"] = reason
            else:
                target.finish_reason = reason  # type: ignore[attr-defined]
            return
        except (AttributeError, TypeError):
            continue


def _synthesize_tool_call_for_gigachat(
    response: Any, tools: list[dict]
) -> dict:
    """Walk response.choices; turn GigaChat plain-text finalizations into
    native ``tool_calls`` envelopes. Returns counters of which level fired.

    Three levels (each tried in order, first hit wins for that choice):

    L1: Form 3 — parse JSON in content (optionally fenced) and match against
        declared tools via ``_detect_tool_call_from_payload``.
    L2: Form 4 — replace ``<|superquote|>`` tokens, then retry L1.
    L3: Forms 1/2 (or anything left) — if ``submit_solution`` is declared,
        wrap content text into a ``submit_solution`` tool_call to finalize.

    Skipped if response already has native ``tool_calls`` (model behaved correctly).
    """
    stats = {"l1_fenced_json": 0, "l2_superquote": 0, "l3_submit_wrap": 0, "skipped": 0}
    if not tools:
        return stats

    choices = getattr(response, "choices", None)
    if choices is None and isinstance(response, dict):
        choices = response.get("choices")
    if not choices:
        return stats

    has_submit_solution = _find_tool_by_name("submit_solution", tools) is not None

    for choice in choices:
        message = getattr(choice, "message", None)
        if message is None and isinstance(choice, dict):
            message = choice.get("message")
        if message is None:
            continue

        # Skip if real native tool_calls already present.
        existing = (
            getattr(message, "tool_calls", None)
            if not isinstance(message, dict)
            else message.get("tool_calls")
        )
        if existing:
            stats["skipped"] += 1
            continue

        content = (
            getattr(message, "content", None)
            if not isinstance(message, dict)
            else message.get("content")
        )
        if not isinstance(content, str) or not content.strip():
            continue

        # ── L1: try parsing fenced/raw JSON object (Form 3) ───────────────
        payload = _parse_json_object(content)
        if payload is not None:
            match = _detect_tool_call_from_payload(payload, tools)
            if match is not None:
                name, args_str = match
                if _set_message_tool_call(
                    message, _make_tool_call(name, args_str, prefix="giga_l1")
                ):
                    _set_choice_finish_reason(choice, "tool_calls")
                    stats["l1_fenced_json"] += 1
                    continue

        # ── L2: replace <|superquote|> → " and retry L1 (Form 4) ──────────
        if _SUPERQUOTE_TOKEN in content:
            cleaned = _replace_superquote_tokens(content)
            payload = _parse_json_object(cleaned)
            if payload is not None:
                match = _detect_tool_call_from_payload(payload, tools)
                if match is not None:
                    name, args_str = match
                    if _set_message_tool_call(
                        message, _make_tool_call(name, args_str, prefix="giga_l2")
                    ):
                        _set_choice_finish_reason(choice, "tool_calls")
                        stats["l2_superquote"] += 1
                        continue

        # ── L3: bare plain text — wrap in submit_solution if declared ─────
        # Only fires when submit_solution is one of the declared tools (it
        # always is in our agent setup). Wrapping a non-finalizer content
        # into submit_solution avoids the Undefined-mode retry loop and
        # saves 2-3 backend calls per task.
        if has_submit_solution:
            text = content.strip()
            args = {
                "handoff": text,
                "task_report": text,
                "solution_type": "answer_question",
            }
            if _set_message_tool_call(
                message,
                _make_tool_call("submit_solution", args, prefix="giga_l3"),
            ):
                _set_choice_finish_reason(choice, "tool_calls")
                stats["l3_submit_wrap"] += 1
                continue

    return stats


def _resolve_refs(node: Any, defs: dict, depth: int = 0) -> Any:
    """Inline $ref pointers (`#/$defs/Name`) and strip $defs.

    GigaChat does not accept JSON-Schema references — pydantic-emitted tool
    schemas commonly include $ref/$defs for nested models, which causes a 422.
    Walk the tree, replace any {"$ref": "#/$defs/X"} with the dereferenced
    schema from `defs`, and drop the `$defs` table itself.

    Ported from ai-forever/gpt2giga `common/json_schema.py::resolve_schema_refs`.
    """
    if depth > 64:
        return node
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref.split("/", 2)[-1]
            target = defs.get(name)
            if isinstance(target, dict):
                # Inline the target; preserve any sibling fields (title/description).
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
    """`type: ["string", "null"]` → `type: "string"` (drop null branch).

    GigaChat doesn't accept array-form types or null variants.
    """
    patched = 0
    if isinstance(node, dict):
        t = node.get("type")
        if isinstance(t, list):
            non_null = [x for x in t if x != "null"]
            if non_null:
                node["type"] = non_null[0]
            else:
                node["type"] = "string"
            patched += 1
        for v in node.values():
            patched += _normalize_nullable_types(v)
    elif isinstance(node, list):
        for item in node:
            patched += _normalize_nullable_types(item)
    return patched


def _flatten_anyof_oneof(node: Any) -> int:
    """Replace anyOf/oneOf with the first non-null variant; merge metadata.

    Ported from ai-forever/gpt2giga. GigaChat doesn't support anyOf/oneOf
    in tool parameter schemas — pydantic emits them for `Optional[T]`.
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
                # Inline chosen variant; preserve siblings (description, etc.).
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


# Built-in GigaChat function names that shadow user tools if reused.
# Sourced from https://developers.sber.ru/docs/ru/gigachat/api/function-calling
_GIGACHAT_RESERVED_FUNCTION_NAMES = frozenset({
    "text2image",
    "text2model3d",
    "get_file_content",
    "web_search",
})


def _remap_reserved_tool_names(tools: list[dict]) -> dict[str, str]:
    """Rename tools whose name collides with a GigaChat built-in function.

    Returns a {original_name: remapped_name} dict so the post-call hook (if
    needed in the future) can map back. In the current pipeline we only
    rename outgoing — GigaChat doesn't reuse the name in tool_calls beyond
    what we pass, so a one-way rename is enough.

    Ported from ai-forever/gpt2giga (reserved name handling in
    `convert_tool_to_giga_functions`).
    """
    remap: dict[str, str] = {}
    for tool in tools:
        fn = tool.get("function")
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if isinstance(name, str) and name in _GIGACHAT_RESERVED_FUNCTION_NAMES:
            new_name = f"__user_{name}"
            fn["name"] = new_name
            remap[name] = new_name
    return remap


def _normalize_gigachat_tools(tools: list[dict]) -> dict:
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


class ProviderNormalizer(CustomLogger):
    """Pre/post-call hooks applied by LiteLLM Proxy around upstream calls."""

    async def async_pre_call_hook(
        self,
        user_api_key_dict,
        cache,
        data: dict,
        call_type: str,
    ) -> dict:
        model = (data.get("model") or "").lower()

        # ── GigaChat: strict JSON Schema for tools ────────────────────────
        if model.startswith("gigachat"):
            tools = data.get("tools") or []
            stats = _normalize_gigachat_tools(tools)
            logger.info(
                "ProviderNormalizer[gigachat]: tools=%d strict_stripped=%d keywords_stripped=%d",
                stats["tools"], stats["strict_stripped"], stats["keywords_stripped"],
            )
            # Empty content also breaks GigaChat in some assistant-only-tool_calls
            # corner cases (same root cause as Yandex).
            messages = data.get("messages") or []
            patched = _backfill_empty_content(messages)
            if patched:
                logger.info(
                    "ProviderNormalizer[gigachat]: %d/%d messages patched for empty content",
                    patched, len(messages),
                )

        # ── Yandex Cloud: empty `content` is rejected ─────────────────────
        if model.startswith("yandex"):
            messages = data.get("messages") or []
            patched = _backfill_empty_content(messages)
            logger.info(
                "ProviderNormalizer[yandex]: %d messages patched (of %d total)",
                patched, len(messages),
            )
            # Stash declared tools on the proxy logging metadata so the
            # post-call hook can synthesize tool_calls from JSON-in-content.
            # ``data`` is the same dict instance reused after upstream call
            # in some LiteLLM versions, but to be safe we pass via cache too.
            try:
                if cache is not None and hasattr(cache, "set"):
                    pass  # cache may not be a kv-store; rely on data
            except Exception:
                pass

        return data

    async def async_post_call_success_hook(
        self,
        data: dict,
        user_api_key_dict,
        response: Any,
    ) -> Any:
        model = (data.get("model") or "").lower()

        # ── Yandex: synthesize tool_calls from JSON in content ────────────
        if model.startswith("yandex"):
            tools = data.get("tools") or []
            patched = _synthesize_tool_calls_for_yandex(response, tools)
            if patched:
                logger.info(
                    "ProviderNormalizer[yandex]: synthesized tool_calls in %d choice(s)",
                    patched,
                )

        # ── GigaChat: rescue plain-text/fenced/superquote finalizations ───
        # GigaChat Lite/Pro/Max often return finalization as plain text in
        # ``content`` instead of native ``tool_calls`` (4 observed forms —
        # see docstring of ``_synthesize_tool_call_for_gigachat``). Without
        # this hook the agent loops via Undefined-mode tech feedback,
        # burning ~6-7k tokens per retry cycle (3-4 cycles on Max).
        if model.startswith("gigachat"):
            tools = data.get("tools") or []
            stats = _synthesize_tool_call_for_gigachat(response, tools)
            total = stats["l1_fenced_json"] + stats["l2_superquote"] + stats["l3_submit_wrap"]
            if total:
                logger.info(
                    "ProviderNormalizer[gigachat]: synthesized tool_calls — "
                    "L1_fenced=%d L2_superquote=%d L3_submit_wrap=%d",
                    stats["l1_fenced_json"], stats["l2_superquote"], stats["l3_submit_wrap"],
                )

        return response


# LiteLLM Proxy expects a module-level instance to use as the callback.
proxy_handler_instance = ProviderNormalizer()
