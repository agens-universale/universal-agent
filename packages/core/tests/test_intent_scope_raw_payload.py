from gc_core.config_modules.intent_scope import (
    DEFAULT_INTENT_SCOPE_MAX_RAW_RECORD_CHARS,
)
from gc_core.intent_scope.raw_payload import truncate_raw_tool_payload


def test_truncate_raw_tool_payload_caps_output():
    huge = "x" * 20_000
    args, out = truncate_raw_tool_payload(
        {"task_report": "ok"},
        huge,
        max_chars=5000,
    )
    assert args == {"task_report": "ok"}
    assert isinstance(out, str)
    assert len(out) < 20_000
    assert "[TEXT CUTTED]" in out


def test_truncate_raw_tool_payload_default_constant():
    args, out = truncate_raw_tool_payload(
        {},
        "y" * (DEFAULT_INTENT_SCOPE_MAX_RAW_RECORD_CHARS + 1000),
        max_chars=DEFAULT_INTENT_SCOPE_MAX_RAW_RECORD_CHARS,
    )
    assert isinstance(out, str)
    assert len(out) <= DEFAULT_INTENT_SCOPE_MAX_RAW_RECORD_CHARS + 20


def test_truncate_raw_tool_payload_preserves_small_payload():
    payload = {"ok": True, "items": [1, 2]}
    args, out = truncate_raw_tool_payload(
        {"a": 1},
        payload,
        max_chars=5000,
    )
    assert args == {"a": 1}
    assert out == payload
