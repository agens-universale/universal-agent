from tools.arguments_validation import WrongFunctionArgumentsException


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
