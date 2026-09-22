"""Optional tool parameters: omit vs empty string (strict OpenAI schemas).

Authors: keep optional *string* property descriptions semantic only (what the field does).
Do not write \"If omitted\" / \"pass empty string\" in tool_description — build_tools_data
appends OPTIONAL_OMIT_HINT or OPTIONAL_EMPTY_AS_OMIT_HINT via augment_optional_property_descriptions.
"""

from __future__ import annotations

import copy
from typing import Any

# LLM-facing hints for semantically optional string fields (must match JSON required).
OPTIONAL_OMIT_HINT = (
    "When unused, omit this field. Empty string \"\" is also accepted and treated as unused."
)
OPTIONAL_EMPTY_AS_OMIT_HINT = (
    "When unused, pass empty string \"\" (field omit is unavailable under strict OpenAI tool schema)."
)

_OPTIONAL_HINTS = (OPTIONAL_OMIT_HINT, OPTIONAL_EMPTY_AS_OMIT_HINT)

# On a property schema: empty string is a real value, not “unused” (excluded from x-optional-keys).
# All x-* keys are internal bookkeeping and are stripped before a request goes out —
# see utils.schema_markers.schema_without_internal_markers.
X_PRESERVE_EMPTY_STRING = "x-preserve-empty-string"
# Semantically optional for local validation, but the omit/empty hint must not be
# appended. Injected fields (reason) stay required on the wire so the model fills
# them; telling it to omit would cancel that contract.
X_SUPPRESS_OPTIONAL_HINT = "x-suppress-optional-hint"


def optional_property_description(base: str, *, openai_strict: bool) -> str:
    """Append optional-field hint aligned with whether strict schema is active."""
    hint = OPTIONAL_EMPTY_AS_OMIT_HINT if openai_strict else OPTIONAL_OMIT_HINT
    base = (base or "").strip()
    if not base:
        return hint
    if any(h in base for h in _OPTIONAL_HINTS):
        return base
    return f"{base.rstrip('.')}. {hint}"


def _property_preserves_empty_string(prop_schema: Any) -> bool:
    return isinstance(prop_schema, dict) and bool(prop_schema.get(X_PRESERVE_EMPTY_STRING))


def resolve_optional_property_keys(params_schema: dict[str, Any]) -> list[str]:
    """
    Keys that are semantically optional (may be omitted or sent as \"\" / null).

    Under OpenAI strict mode, ``required`` lists every property; use ``x-optional-keys``
    saved before strict normalization.
    """
    if not isinstance(params_schema, dict):
        return []
    props = params_schema.get("properties")
    if not isinstance(props, dict):
        return []

    def _eligible(name: str) -> bool:
        prop = props.get(name)
        return not _property_preserves_empty_string(prop)

    explicit = params_schema.get("x-optional-keys")
    if isinstance(explicit, list):
        return [k for k in explicit if isinstance(k, str) and k in props and _eligible(k)]

    required = params_schema.get("required")
    if not isinstance(required, list):
        required = []
    required_set = {k for k in required if isinstance(k, str)}
    return [k for k in props if k not in required_set and _eligible(k)]


def _is_string_like_property(prop_schema: Any) -> bool:
    if not isinstance(prop_schema, dict):
        return False
    schema_type = prop_schema.get("type")
    if schema_type == "string":
        return True
    if isinstance(schema_type, list) and "string" in schema_type:
        return True
    for branch in prop_schema.get("anyOf") or prop_schema.get("oneOf") or []:
        if isinstance(branch, dict) and branch.get("type") == "string":
            return True
    return False


def _accepts_empty_string(prop_schema: Any) -> bool:
    """Пройдёт ли "" валидацию против под-схемы этого свойства.

    Обе подсказки обещают модели, что пустая строка означает «не задано». Для
    свойства с ``enum`` (или ``minLength``, ``pattern``, ``format``) это обещание
    ложно: Draft7 отвергает "" раньше, чем сработает нормализация. Модель честно
    следует подсказке и получает ошибку аргументов — наблюдалось на живом
    прогоне с ``fs_write_file.newline`` (17.08.2026).

    Поэтому подсказку вешаем только туда, где она правдива.
    """
    if not isinstance(prop_schema, dict):
        return False
    try:
        from jsonschema import Draft7Validator

        return Draft7Validator(prop_schema).is_valid("")
    except Exception:  # noqa: BLE001
        # Схему не удалось разобрать — не отнимаем подсказку, поведение
        # остаётся прежним.
        return True


def seed_optional_keys(schema: dict[str, Any]) -> list[str]:
    """
    Ensure ``x-optional-keys`` exists on an object node and return it.

    Seeded from the keys absent from ``required`` (minus properties that treat an
    empty string as a real value). Idempotent: an existing list is kept as is.
    Single source of truth for "which keys may be omitted" — callers that make a
    property semantically optional (strict expansion, field injection) go through here.
    """
    existing = schema.get("x-optional-keys")
    if isinstance(existing, list):
        return existing
    props = schema.get("properties")
    if not isinstance(props, dict):
        props = {}
    required_before = schema.get("required")
    if not isinstance(required_before, list):
        required_before = []
    seeded = [
        k
        for k in props
        if k not in required_before and not _property_preserves_empty_string(props[k])
    ]
    schema["x-optional-keys"] = seeded
    return seeded


def mark_property_optional(schema: dict[str, Any], key: str) -> None:
    """Record ``key`` as semantically optional (omittable) for local validation."""
    optional = seed_optional_keys(schema)
    if key not in optional:
        schema["x-optional-keys"] = [*optional, key]


def apply_openai_strict_object(schema: dict[str, Any]) -> None:
    """
    OpenAI strict mode for one object node: every property key is required;
    remember semantic optional keys before expansion.
    """
    if schema.get("type") != "object":
        return
    props = schema.get("properties")
    if not isinstance(props, dict):
        props = {}
        schema["properties"] = props
    if not props:
        schema["x-openai-strict"] = True
        schema["required"] = []
        schema["additionalProperties"] = False
        return
    seed_optional_keys(schema)
    schema["x-openai-strict"] = True
    schema["required"] = list(props.keys())
    # OpenAI strict always requires explicit false (Pydantic may emit true for dict types).
    schema["additionalProperties"] = False


def apply_openai_strict_parameters(params: dict[str, Any]) -> None:
    """OpenAI strict mode for top-level tool parameters (object root)."""
    if not isinstance(params, dict):
        return
    apply_openai_strict_object(params)


def _rewrite_semantic_required(schema: Any) -> None:
    """In-place: on strict/optional-marked objects, set required to semantic keys only."""
    if not isinstance(schema, dict):
        return

    is_object = schema.get("type") == "object" or isinstance(schema.get("properties"), dict)
    if is_object and (
        isinstance(schema.get("x-optional-keys"), list) or schema.get("x-openai-strict")
    ):
        props = schema.get("properties")
        if isinstance(props, dict):
            optional = set(resolve_optional_property_keys(schema))
            schema["required"] = [k for k in props if k not in optional]
        else:
            schema["required"] = []

    for sub in _nested_schemas(schema):
        _rewrite_semantic_required(sub)


def _has_optional_markers(schema: Any) -> bool:
    """True if the schema (or any nested subschema) carries strict/optional markers."""
    if not isinstance(schema, dict):
        return False
    if schema.get("x-openai-strict") or isinstance(schema.get("x-optional-keys"), list):
        return True
    return any(_has_optional_markers(sub) for sub in _nested_schemas(schema))


def _nested_schemas(schema: dict):
    """Direct subschemas: properties, items, anyOf/oneOf/allOf branches, $defs/definitions."""
    props = schema.get("properties")
    if isinstance(props, dict):
        yield from props.values()
    items = schema.get("items")
    if isinstance(items, dict):
        yield items
    elif isinstance(items, list):
        yield from items
    for key in ("anyOf", "oneOf", "allOf"):
        branches = schema.get(key)
        if isinstance(branches, list):
            yield from branches
    for defs_key in ("$defs", "definitions"):
        defs = schema.get(defs_key)
        if isinstance(defs, dict):
            yield from defs.values()


def parameters_schema_for_local_validation(params: dict[str, Any]) -> dict[str, Any]:
    """
    Schema for local Draft7 validation under OpenAI strict tools mode.

    API schema keeps full ``required``; local validation uses semantic required
    (properties − x-optional-keys) so omit of optional keys is allowed.
    """
    if not isinstance(params, dict):
        return params
    if not _has_optional_markers(params):
        return params
    out = copy.deepcopy(params)
    _rewrite_semantic_required(out)
    return out


def augment_optional_property_descriptions(
    spec: dict[str, Any],
    *,
    openai_strict: bool = False,
) -> None:
    """In-place: add optional-field hint to optional string-like properties."""
    if not isinstance(spec, dict):
        return
    params = spec.get("parameters")
    if not isinstance(params, dict):
        return
    strict = openai_strict or bool(params.get("x-openai-strict"))
    props = params.get("properties")
    if not isinstance(props, dict):
        return

    for name in resolve_optional_property_keys(params):
        prop = props.get(name)
        if not _is_string_like_property(prop):
            continue
        # Injected fields are required on the wire. The omit/empty hint would
        # tell the model to skip a field the schema just demanded.
        if prop.get(X_SUPPRESS_OPTIONAL_HINT):
            continue
        # Не обещаем пустую строку там, где схема её не примет (enum, minLength,
        # pattern): подсказка увела бы модель в гарантированную ошибку аргументов.
        if not _accepts_empty_string(prop):
            continue
        prop["description"] = optional_property_description(
            prop.get("description", ""),
            openai_strict=strict,
        )


def coalesce_optional_string(value: Any) -> str | None:
    """Treat None and \"\" as absent for optional string tool parameters."""
    if value is None or value == "":
        return None
    return value if isinstance(value, str) else None


def normalize_optional_empty_as_omit(
    parsed_args: dict[str, Any],
    params_schema: dict[str, Any],
) -> dict[str, Any]:
    """
    Drop semantically optional keys whose value is \"\" or null.

    Call after JSON Schema validation (local validate uses semantic required under strict).
    """
    optional_keys = resolve_optional_property_keys(params_schema)
    if not optional_keys:
        return parsed_args

    out = dict(parsed_args)
    for key in optional_keys:
        if key not in out:
            continue
        value = out[key]
        if value is None or value == "":
            del out[key]
    return out


__all__ = [
    "X_PRESERVE_EMPTY_STRING",
    "X_SUPPRESS_OPTIONAL_HINT",
    "OPTIONAL_OMIT_HINT",
    "OPTIONAL_EMPTY_AS_OMIT_HINT",
    "optional_property_description",
    "resolve_optional_property_keys",
    "apply_openai_strict_object",
    "apply_openai_strict_parameters",
    "parameters_schema_for_local_validation",
    "augment_optional_property_descriptions",
    "coalesce_optional_string",
    "normalize_optional_empty_as_omit",
]
