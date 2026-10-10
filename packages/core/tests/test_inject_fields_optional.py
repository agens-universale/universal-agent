import pytest
from gc_core.tools.arguments_validation import (
    WrongFunctionArgumentsException,
    parse_and_validate_tool_arguments,
)
from gc_core.tools.inject_tool_fields import apply_injected_fields_to_spec
from gc_core.execution.tool_schema import (
    OPTIONAL_EMPTY_AS_OMIT_HINT,
    OPTIONAL_OMIT_HINT,
    X_SUPPRESS_OPTIONAL_HINT,
    apply_openai_strict_parameters,
    augment_optional_property_descriptions,
    resolve_optional_property_keys,
)

INJECT = {"reason": "One short sentence explaining the purpose of this call."}


def _spec() -> dict:
    return {
        "type": "function",
        "name": "fs_read_file",
        "description": "Read a file.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    }


def test_injected_reason_is_marked_optional():
    spec = _spec()
    apply_injected_fields_to_spec(spec, INJECT)
    params = spec["parameters"]

    assert params["required"][0] == "reason"  # API schema still demands it
    assert "reason" in resolve_optional_property_keys(
        params
    )  # local validation does not


def test_call_without_reason_validates():
    spec = _spec()
    apply_injected_fields_to_spec(spec, INJECT)

    args = parse_and_validate_tool_arguments(
        tool_name="fs_read_file",
        arguments={"path": "/testbed/a.py"},
        tool_spec=spec,
        allow_empty_for_empty_schema=False,
    )
    assert args == {"path": "/testbed/a.py"}


def test_call_with_reason_still_validates():
    spec = _spec()
    apply_injected_fields_to_spec(spec, INJECT)

    args = parse_and_validate_tool_arguments(
        tool_name="fs_read_file",
        arguments={"path": "/testbed/a.py", "reason": "read the module"},
        tool_spec=spec,
        allow_empty_for_empty_schema=False,
    )
    assert args["reason"] == "read the module"


@pytest.mark.parametrize("openai_strict", [False, True])
def test_injected_reason_does_not_get_omit_hint(openai_strict):
    spec = _spec()
    apply_injected_fields_to_spec(spec, INJECT)
    if openai_strict:
        apply_openai_strict_parameters(spec["parameters"])
    augment_optional_property_descriptions(spec, openai_strict=openai_strict)

    prop = spec["parameters"]["properties"]["reason"]
    assert prop[X_SUPPRESS_OPTIONAL_HINT] is True
    assert prop["description"] == INJECT["reason"]
    assert OPTIONAL_OMIT_HINT not in prop["description"]
    assert OPTIONAL_EMPTY_AS_OMIT_HINT not in prop["description"]
    assert spec["parameters"]["required"][0] == "reason"
    assert "reason" in resolve_optional_property_keys(spec["parameters"])


def test_real_required_field_still_enforced():
    spec = _spec()
    apply_injected_fields_to_spec(spec, INJECT)

    with pytest.raises(WrongFunctionArgumentsException):
        parse_and_validate_tool_arguments(
            tool_name="fs_read_file",
            arguments={"reason": "no path given"},
            tool_spec=spec,
            allow_empty_for_empty_schema=False,
        )
