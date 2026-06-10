"""Optional tool parameters: omit vs empty string (strict OpenAI schemas).

Authors: keep optional *string* property descriptions semantic only (what the field does).
Do not write \"If omitted\" / \"pass empty string\" in tool_description — build_tools_data
appends OPTIONAL_OMIT_HINT or OPTIONAL_EMPTY_AS_OMIT_HINT via augment_optional_property_descriptions.
"""

from __future__ import annotations

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
X_PRESERVE_EMPTY_STRING = "x-preserve-empty-string"


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
    if "x-optional-keys" not in schema:
        required_before = schema.get("required")
        if not isinstance(required_before, list):
            required_before = []
        schema["x-optional-keys"] = [
            k
            for k in props
            if k not in required_before and not _property_preserves_empty_string(props[k])
        ]
    schema["x-openai-strict"] = True
    schema["required"] = list(props.keys())
    # OpenAI strict always requires explicit false (Pydantic may emit true for dict types).
    schema["additionalProperties"] = False


def apply_openai_strict_parameters(params: dict[str, Any]) -> None:
    """OpenAI strict mode for top-level tool parameters (object root)."""
    if not isinstance(params, dict):
        return
    apply_openai_strict_object(params)


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

    Call after JSON Schema validation so strict mode still receives all keys during validate.
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
    "OPTIONAL_OMIT_HINT",
    "OPTIONAL_EMPTY_AS_OMIT_HINT",
    "optional_property_description",
    "resolve_optional_property_keys",
    "apply_openai_strict_object",
    "apply_openai_strict_parameters",
    "augment_optional_property_descriptions",
    "coalesce_optional_string",
    "normalize_optional_empty_as_omit",
]
