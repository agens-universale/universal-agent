from gc_core.tools.arguments_validation import WrongFunctionArgumentsException


def test_anyof_object_shown_as_object_not_any():
    schema = {"parameters": {"type": "object", "properties": {
        "attributes": {"anyOf": [{"type": "object"}, {"type": "null"}]},
    }, "required": [], "additionalProperties": False}}
    exc = WrongFunctionArgumentsException(
        message="bad", received_args="{}", expected_schema=schema,
        tool_name="t",
    )
    fb = exc.to_llm_feedback()
    assert '"attributes": <object>' in fb
    assert '"attributes": <any>' not in fb


def test_received_arguments_are_not_duplicated_in_model_feedback():
    raw = "unique-problematic-payload-" * 400
    exc = WrongFunctionArgumentsException(
        message="validation says field handoff is required",
        received_args=raw,
        tool_name="turn_finish",
        extra_hint="Retry with valid JSON.",
    )
    feedback = exc.to_llm_feedback()
    output = exc.to_tool_output()
    assert raw not in feedback
    assert raw not in output
    assert "validation says field handoff is required" in feedback
    assert "Retry with valid JSON." in feedback
    assert "You sent:" not in feedback
    assert len(output) < 1000
