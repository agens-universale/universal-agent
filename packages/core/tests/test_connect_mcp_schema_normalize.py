"""Regression tests for tools.connect_mcp.agent_tools schema normalization.

Reproduces a bug where _normalize_schema_recursive forced `type: "string"` on
parameter schemas that legitimately use `anyOf` / `oneOf` / `allOf` (e.g.
`int | None` Python type hints in MCP tool signatures), making JSON Schema
validation impossible because the validator requires both `type` AND `anyOf`
to match.
"""

from __future__ import annotations

import pytest

from gc_core.tools.arguments_validation import (
    parse_and_validate_tool_arguments,
)
from gc_core.tools.connect_mcp.agent_tools import (
    _normalize_schema_recursive,
    normalize_tool_spec_for_agent,
)


def test_normalize_preserves_anyof_without_forcing_type() -> None:
    """`anyOf` schema should not get a contradictory `type: string` slapped on."""
    raw = {
        "anyOf": [{"type": "integer"}, {"type": "null"}],
        "default": None,
        "title": "Limit",
    }
    normalized = _normalize_schema_recursive(raw, param_name="limit")

    assert "anyOf" in normalized
    assert normalized["anyOf"][0]["type"] == "integer"
    assert normalized["anyOf"][1]["type"] == "null"
    # The bug: previously normalized["type"] was forced to "string".
    # After fix: either no `type`, or `type` value compatible with anyOf — but NOT
    # an unrelated default like "string".
    if "type" in normalized:
        assert normalized["type"] != "string", (
            "type='string' contradicts anyOf=[integer, null] and breaks validation"
        )


def test_normalize_preserves_oneof_without_forcing_type() -> None:
    raw = {
        "oneOf": [{"type": "integer"}, {"type": "null"}],
        "title": "X",
    }
    normalized = _normalize_schema_recursive(raw, param_name="x")
    assert "oneOf" in normalized
    if "type" in normalized:
        assert normalized["type"] != "string"


def test_normalize_preserves_allof_without_forcing_type() -> None:
    raw = {
        "allOf": [{"type": "integer"}],
        "title": "Y",
    }
    normalized = _normalize_schema_recursive(raw, param_name="y")
    assert "allOf" in normalized
    if "type" in normalized:
        assert normalized["type"] != "string"


def test_normalize_leaves_untyped_node_free() -> None:
    """Узел без type/properties/items/anyOf — «любое значение», не строка."""
    raw = {"description": "Some param", "title": "Z"}
    normalized = _normalize_schema_recursive(raw, param_name="z")
    assert "type" not in normalized


def test_normalize_object_with_anyof_property() -> None:
    """Top-level object has properties; one property uses anyOf."""
    raw = {
        "type": "object",
        "properties": {
            "limit": {
                "anyOf": [{"type": "integer"}, {"type": "null"}],
                "default": None,
                "title": "Limit",
            },
        },
        "required": [],
    }
    normalized = _normalize_schema_recursive(raw)
    limit_schema = normalized["properties"]["limit"]
    assert "anyOf" in limit_schema
    if "type" in limit_schema:
        assert limit_schema["type"] != "string"


def test_full_tool_spec_with_anyof_param_stays_valid() -> None:
    """End-to-end: normalize_tool_spec_for_agent doesn't break anyOf params."""
    tool_spec = {
        "type": "function",
        "name": "query_xlsx",
        "description": "Filter rows.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "title": "Path"},
                "limit": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                    "default": None,
                    "title": "Limit",
                },
            },
            "required": ["path"],
        },
        "strict": True,
    }
    normalized = normalize_tool_spec_for_agent(tool_spec)
    limit = normalized["parameters"]["properties"]["limit"]
    assert "anyOf" in limit
    assert limit["anyOf"][0]["type"] == "integer"
    assert limit["anyOf"][1]["type"] == "null"
    if "type" in limit:
        assert limit["type"] != "string"


def test_existing_explicit_type_is_preserved() -> None:
    """Regression: a schema that already declares its own `type` should not be touched."""
    raw = {"type": "integer", "title": "N"}
    normalized = _normalize_schema_recursive(raw, param_name="n")
    assert normalized["type"] == "integer"


def test_object_without_type_still_inferred() -> None:
    """Regression: dict with `properties` but no `type` should still be inferred as object."""
    raw = {"properties": {"x": {"type": "string"}}}
    normalized = _normalize_schema_recursive(raw, param_name="o")
    assert normalized["type"] == "object"


def test_array_without_type_still_inferred() -> None:
    """Regression: dict with `items` but no `type` should still be inferred as array."""
    raw = {"items": {"type": "string"}}
    normalized = _normalize_schema_recursive(raw, param_name="a")
    assert normalized["type"] == "array"


def test_inline_defs_ref_for_openai_strict_workbook() -> None:
    """Bare ``$ref`` properties lack additionalProperties; inlining fixes OpenAI strict."""
    tool_spec = {
        "type": "function",
        "name": "mcp_office_create_xlsx_file",
        "description": "Generate xlsx.",
        "parameters": {
            "type": "object",
            "$defs": {
                "WorkbookSpec": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "sheets": {
                            "type": "array",
                            "items": {"type": "object"},
                        },
                    },
                    "required": ["sheets"],
                },
            },
            "properties": {
                "path": {"type": "string", "title": "Path"},
                "workbook": {"$ref": "#/$defs/WorkbookSpec"},
                "overwrite": {
                    "type": "boolean",
                    "default": False,
                    "title": "Overwrite",
                },
            },
            "required": ["path", "workbook"],
        },
    }
    normalized = normalize_tool_spec_for_agent(tool_spec)
    workbook = normalized["parameters"]["properties"]["workbook"]
    assert workbook["type"] == "object"
    assert workbook["additionalProperties"] is False
    assert "sheets" in workbook["properties"]
    assert "$ref" not in workbook
    assert "$defs" not in normalized["parameters"]


def test_nested_object_required_includes_all_properties_for_openai_strict() -> (
    None
):
    """Pydantic optional fields must appear in nested ``required`` under strict mode."""
    cell_style = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "range": {"type": "string", "title": "Range"},
            "align": {
                "anyOf": [
                    {"enum": ["left", "center", "right"], "type": "string"},
                    {"type": "null"},
                ],
                "default": None,
                "title": "Align",
            },
            "bold": {
                "anyOf": [{"type": "boolean"}, {"type": "null"}],
                "default": None,
                "title": "Bold",
            },
        },
        "required": ["range"],
    }
    tool_spec = {
        "type": "function",
        "name": "mcp_office_create_xlsx_file",
        "description": "Generate xlsx.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "title": "Path"},
                "workbook": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "sheets": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "name": {"type": "string"},
                                    "cell_styles": {
                                        "type": "array",
                                        "items": cell_style,
                                    },
                                },
                                "required": ["name", "cell_styles"],
                            },
                        },
                    },
                    "required": ["sheets"],
                },
            },
            "required": ["path", "workbook"],
        },
    }
    normalized = normalize_tool_spec_for_agent(tool_spec, openai_strict=True)
    cell_items = normalized["parameters"]["properties"]["workbook"][
        "properties"
    ]["sheets"]["items"]["properties"]["cell_styles"]["items"]
    assert set(cell_items["required"]) == {"range", "align", "bold"}
    assert cell_items["x-optional-keys"] == ["align", "bold"]
    assert set(normalized["parameters"]["required"]) == {"path", "workbook"}


def test_anyof_array_branch_items_stay_free_and_drop_strict() -> None:
    """``list | None`` от Pydantic даёт ``items: {}`` внутри anyOf: элементы —
    любые значения, а инструмент уходит без strict."""
    from gc_core.utils.connect_backend import project_tool_specs

    tool_spec = {
        "type": "function",
        "name": "mcp_office_query_xlsx",
        "description": "Query xlsx.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "title": "Path"},
                "filter": {
                    "anyOf": [
                        {"items": {}, "type": "array"},
                        {"type": "null"},
                    ],
                    "default": None,
                    "title": "Filter",
                },
            },
            "required": ["path"],
        },
    }
    normalized = normalize_tool_spec_for_agent(tool_spec)
    array_branch = normalized["parameters"]["properties"]["filter"]["anyOf"][0]
    assert array_branch["type"] == "array"
    assert "type" not in array_branch["items"]
    (sent,) = project_tool_specs(
        [normalized], openai_strict=True, send_tool_strict=True
    )
    assert sent["strict"] is False


def test_recursive_defs_ref_keeps_defs_for_openai_strict() -> None:
    """Recursive Pydantic models must not leave dangling ``$ref`` without ``$defs``."""
    tool_spec = {
        "type": "function",
        "name": "mcp_office_query_xlsx",
        "description": "Query xlsx.",
        "parameters": {
            "type": "object",
            "$defs": {
                "FilterClause": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "col": {"type": "string", "title": "Col"},
                        "op": {"type": "string", "title": "Op"},
                    },
                    "required": ["col", "op"],
                },
                "OrFilterGroup": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "or": {
                            "type": "array",
                            "items": {
                                "anyOf": [
                                    {"$ref": "#/$defs/FilterClause"},
                                    {"$ref": "#/$defs/OrFilterGroup"},
                                ],
                            },
                            "title": "Or",
                        },
                    },
                    "required": ["or"],
                },
            },
            "properties": {
                "path": {"type": "string", "title": "Path"},
                "filter": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {
                                "anyOf": [
                                    {"$ref": "#/$defs/FilterClause"},
                                    {"$ref": "#/$defs/OrFilterGroup"},
                                ],
                            },
                        },
                        {"type": "null"},
                    ],
                    "default": None,
                    "title": "Filter",
                },
            },
            "required": ["path"],
        },
    }
    normalized = normalize_tool_spec_for_agent(tool_spec)
    params = normalized["parameters"]
    filter_schema = params["properties"]["filter"]
    array_branch = filter_schema["anyOf"][0]
    or_group_branch = array_branch["items"]["anyOf"][1]
    assert or_group_branch["type"] == "object"
    nested_or = or_group_branch["properties"]["or"]["items"]["anyOf"][1]
    assert nested_or == {"$ref": "#/$defs/OrFilterGroup"}
    assert "FilterClause" in params["$defs"]
    assert "OrFilterGroup" in params["$defs"]
    assert params["$defs"]["OrFilterGroup"]["additionalProperties"] is False


def test_anyof_array_object_items_stay_open() -> None:
    """``list[dict] | None``: элементы — свободные объекты, их не закрываем.

    Закрытый объект без ``properties`` не принял бы ни одного ключа. OpenAI
    strict открытый объект не принимает — такой инструмент уходит без strict.
    """
    tool_spec = {
        "type": "function",
        "name": "mcp_office_query_xlsx",
        "description": "Query xlsx.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "title": "Path"},
                "sort": {
                    "anyOf": [
                        {
                            "items": {
                                "additionalProperties": True,
                                "type": "object",
                            },
                            "type": "array",
                        },
                        {"type": "null"},
                    ],
                    "default": None,
                    "title": "Sort",
                },
            },
            "required": ["path"],
        },
    }
    for openai_strict in (False, True):
        normalized = normalize_tool_spec_for_agent(
            tool_spec, openai_strict=openai_strict
        )
        items = normalized["parameters"]["properties"]["sort"]["anyOf"][0][
            "items"
        ]
        assert items["additionalProperties"] is True
        assert "x-openai-strict" not in items


def _create_issue_spec() -> dict:
    """Как ``create_issue`` у Jira MCP: произвольные поля задачи — ``dict``."""
    return {
        "type": "function",
        "name": "mcp_jira_create_issue",
        "description": "Create issue.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "fields": {"type": "object", "additionalProperties": True},
                "labels": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
                "extra": {"type": "object"},
                "meta": {
                    "type": "object",
                    "properties": {"a": {"type": "string"}},
                },
            },
            "required": ["project"],
        },
    }


@pytest.mark.parametrize("openai_strict", [False, True])
def test_free_object_param_keeps_its_keys(openai_strict: bool) -> None:
    """Регресс: ``fields: dict`` превращался в ``{}`` с ``additionalProperties:
    false`` — валидатор отказывал в любом ключе (``customfield_17500``)."""
    normalized = normalize_tool_spec_for_agent(
        _create_issue_spec(), openai_strict=openai_strict
    )
    props = normalized["parameters"]["properties"]
    assert props["fields"]["additionalProperties"] is True
    assert props["labels"]["additionalProperties"] == {"type": "string"}
    # Объект без properties и без additionalProperties — по JSON Schema открыт.
    assert props["extra"]["additionalProperties"] is True
    # Объект с перечисленными ключами закрывается, как и раньше.
    assert props["meta"]["additionalProperties"] is False
    # Без пустых properties/required: OpenAI без strict читает такой объект
    # как пустой и шлёт `{}` (gpt-5.4-mini, проверено живым запросом).
    for name in ("fields", "labels", "extra"):
        assert "properties" not in props[name]
        assert "required" not in props[name]

    args = parse_and_validate_tool_arguments(
        tool_name="mcp_jira_create_issue",
        arguments={
            "project": "PDLC",
            "fields": {"customfield_17500": "x", "priority": {"name": "High"}},
            "labels": {"team": "qa"},
            "extra": {"k": 1},
            "meta": {"a": "b"},
        },
        tool_spec=normalized,
        allow_empty_for_empty_schema=False,
    )
    assert args["fields"] == {
        "customfield_17500": "x",
        "priority": {"name": "High"},
    }
    assert args["labels"] == {"team": "qa"}


def test_free_object_values_still_validated() -> None:
    """Открыт только набор ключей: схема значений (``dict[str, str]``) действует."""
    normalized = normalize_tool_spec_for_agent(_create_issue_spec())
    with pytest.raises(Exception):
        parse_and_validate_tool_arguments(
            tool_name="mcp_jira_create_issue",
            arguments={"project": "PDLC", "labels": {"team": 1}},
            tool_spec=normalized,
            allow_empty_for_empty_schema=False,
        )


@pytest.mark.parametrize("openai_strict", [False, True])
def test_tool_with_free_object_is_sent_without_strict(
    openai_strict: bool,
) -> None:
    """strict требует закрыть каждый объект — инструмент со свободным уходит
    со ``strict: false``, свободный объект остаётся открытым; остальные
    инструменты strict сохраняют."""
    from gc_core.utils.connect_backend import project_tool_specs

    free = normalize_tool_spec_for_agent(_create_issue_spec())
    plain = normalize_tool_spec_for_agent(
        {
            "type": "function",
            "name": "mcp_jira_get_issue",
            "description": "Get issue.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
            },
        }
    )
    sent_free, sent_plain = project_tool_specs(
        [free, plain], openai_strict=openai_strict, send_tool_strict=True
    )
    assert sent_free["strict"] is False
    assert (
        sent_free["parameters"]["properties"]["fields"]["additionalProperties"]
        is True
    )
    assert sent_plain["strict"] is True
    # Исходные спеки проекция не меняет.
    assert free["strict"] is True


def test_non_strict_preserves_server_required_default() -> None:
    """Default (openai_strict=False) keeps the MCP server's own `required`.

    Regression: gc-cli used to force EVERY MCP-tool property into `required`
    (OpenAI strict mode) unconditionally, ignoring backend.openai_strict_tools_schema.
    Non-strict models (e.g. deepseek) then omitted "optional" params like
    base/page/per_page -> WrongFunctionArgumentsException. With the flag off,
    optional params must stay optional.
    """
    tool_spec = {
        "type": "function",
        "name": "mcp_gitverse_list_pull_requests",
        "description": "List PRs.",
        "parameters": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "base": {"type": "string", "description": "Filter (optional)"},
                "page": {"type": "integer", "default": 1},
                "per_page": {"type": "integer", "default": 30},
            },
            "required": ["owner", "repo"],
        },
    }
    ns = normalize_tool_spec_for_agent(
        tool_spec
    )  # default: openai_strict=False
    assert ns["parameters"]["required"] == ["owner", "repo"]
    assert ns.get("strict") is False

    s = normalize_tool_spec_for_agent(tool_spec, openai_strict=True)
    assert set(s["parameters"]["required"]) == {
        "owner",
        "repo",
        "base",
        "page",
        "per_page",
    }
    assert s.get("strict") is True


def _single_param_spec(param_schema, *, name: str = "v") -> dict:
    return {
        "type": "function",
        "name": "mcp_demo_tool",
        "description": "Demo.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {name: param_schema},
            "required": [name],
        },
    }


def _validated(spec: dict, arguments: dict) -> dict:
    return parse_and_validate_tool_arguments(
        tool_name=spec["name"],
        arguments=arguments,
        tool_spec=spec,
        allow_empty_for_empty_schema=False,
    )


@pytest.mark.parametrize("raw", [{}, {"description": "x"}])
@pytest.mark.parametrize("value", [1, True, None, "s", [1], {"a": 1}])
def test_untyped_property_accepts_any_value(raw: dict, value) -> None:
    """Узел без ``type`` по JSON Schema — любое значение, не строка."""
    spec = normalize_tool_spec_for_agent(_single_param_spec(raw))
    assert "type" not in spec["parameters"]["properties"]["v"]
    assert _validated(spec, {"v": value}) == {"v": value}


@pytest.mark.parametrize("openai_strict", [False, True])
def test_nested_untyped_items_accept_numbers(openai_strict: bool) -> None:
    """``List[List]``: ``items: {}`` на глубине принимает числа."""
    raw = {"type": "array", "items": {"type": "array", "items": {}}}
    spec = normalize_tool_spec_for_agent(
        _single_param_spec(raw), openai_strict=openai_strict
    )
    rows = [["2025-03", 4149.4, 2915.4]]
    assert _validated(spec, {"v": rows}) == {"v": rows}


def test_anyof_untyped_branch_stays_untyped() -> None:
    raw = {"anyOf": [{}, {"type": "null"}]}
    normalized = _normalize_schema_recursive(raw, param_name="v")
    assert "type" not in normalized["anyOf"][0]


def test_untyped_enum_accepts_its_values() -> None:
    spec = normalize_tool_spec_for_agent(_single_param_spec({"enum": [1, 2]}))
    assert _validated(spec, {"v": 1}) == {"v": 1}


def test_boolean_true_schema_accepts_any_value() -> None:
    spec = normalize_tool_spec_for_agent(
        {
            "type": "function",
            "name": "mcp_demo_tool",
            "description": "Demo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "v": True,
                    "w": {"type": "array", "items": True},
                },
                "required": ["v", "w"],
            },
        }
    )
    props = spec["parameters"]["properties"]
    assert set(props["v"]) == {"description"}
    assert set(props["w"]["items"]) == {"description"}
    args = {"v": 1, "w": [1]}
    assert _validated(spec, args) == args


@pytest.mark.parametrize(
    "schema",
    [
        {},
        True,
        {"enum": [1]},
        {"type": "object", "additionalProperties": True},
    ],
)
def test_free_node_predicate_true(schema) -> None:
    from gc_core.execution.tool_schema import is_free_node

    assert is_free_node(schema) is True


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "string"},
        {"$ref": "#/$defs/X"},
        {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        False,
    ],
)
def test_free_node_predicate_false(schema) -> None:
    from gc_core.execution.tool_schema import is_free_node

    assert is_free_node(schema) is False


def _project_strict(spec: dict) -> dict:
    from gc_core.utils.connect_backend import project_tool_specs

    (sent,) = project_tool_specs(
        [spec], openai_strict=False, send_tool_strict=True
    )
    return sent


@pytest.mark.parametrize("openai_strict", [False, True])
def test_untyped_node_is_sent_without_strict(openai_strict: bool) -> None:
    """Инструмент со свободным узлом уходит без strict, соседний — со strict."""
    from gc_core.utils.connect_backend import project_tool_specs

    free = normalize_tool_spec_for_agent(_single_param_spec({}))
    typed = normalize_tool_spec_for_agent(
        _single_param_spec({"type": "string"})
    )
    sent_free, sent_typed = project_tool_specs(
        [free, typed], openai_strict=openai_strict, send_tool_strict=True
    )
    assert sent_free["strict"] is False
    assert sent_typed["strict"] is True
    assert free["strict"] is True


def test_free_node_inside_defs_drops_strict() -> None:
    raw = _single_param_spec({"$ref": "#/$defs/V"})
    raw["parameters"]["$defs"] = {"V": {}}
    sent = _project_strict(normalize_tool_spec_for_agent(raw))
    assert sent["strict"] is False


def test_has_free_node_walks_defs() -> None:
    from gc_core.execution.tool_schema import has_free_node

    schema = {"type": "object", "properties": {}, "$defs": {"V": {}}}
    assert has_free_node(schema) is True


def test_anyof_true_branch_drops_strict() -> None:
    raw = _single_param_spec({"anyOf": [True, {"type": "null"}]})
    sent = _project_strict(normalize_tool_spec_for_agent(raw))
    assert sent["strict"] is False


@pytest.mark.parametrize("openai_strict", [False, True])
def test_native_tools_keep_strict(openai_strict: bool) -> None:
    """Страж: схемы встроенных тулов полностью типизированы — проекция не
    снимает с них strict."""
    from gc_core.tools.registry import _collect_builtin_registry
    from gc_core.utils.connect_backend import project_tool_specs
    from gc_testkit.settings import make_settings

    specs = [
        entry["spec"]
        for entry in _collect_builtin_registry(make_settings()).values()
    ]
    sent = project_tool_specs(
        specs, openai_strict=openai_strict, send_tool_strict=True
    )
    assert [(spec["name"], spec.get("strict")) for spec in sent] == [
        (spec["name"], spec.get("strict")) for spec in specs
    ]


def _strict_projected_optional(extra_schema: dict) -> dict:
    """Спека с обязательным ``key`` и необязательным ``extra`` после
    проекции под ``openai_strict``."""
    from gc_core.utils.connect_backend import project_tool_specs

    raw = _single_param_spec({"type": "string"}, name="key")
    raw["parameters"]["properties"]["extra"] = extra_schema
    (sent,) = project_tool_specs(
        [normalize_tool_spec_for_agent(raw)],
        openai_strict=True,
        send_tool_strict=True,
    )
    return sent


def test_optional_untyped_param_gets_skip_hint_under_strict() -> None:
    """Регресс: под strict поле без типа обязательно на проводе, а подсказки
    «пропусти пустой строкой» у него не было — модель выдумывала значение."""
    from gc_core.execution.tool_schema import OPTIONAL_EMPTY_AS_OMIT_HINT

    params = _strict_projected_optional({})["parameters"]
    assert "extra" in params["required"]
    desc = params["properties"]["extra"]["description"]
    assert OPTIONAL_EMPTY_AS_OMIT_HINT in desc


def test_optional_untyped_empty_string_is_omitted() -> None:
    sent = _strict_projected_optional({})
    assert _validated(sent, {"key": "k", "extra": ""}) == {"key": "k"}


def test_optional_untyped_enum_gets_no_hint() -> None:
    from gc_core.execution.tool_schema import (
        OPTIONAL_EMPTY_AS_OMIT_HINT,
        OPTIONAL_OMIT_HINT,
    )

    sent = _strict_projected_optional({"enum": [1, 2]})
    desc = sent["parameters"]["properties"]["extra"].get("description", "")
    assert OPTIONAL_EMPTY_AS_OMIT_HINT not in desc
    assert OPTIONAL_OMIT_HINT not in desc
