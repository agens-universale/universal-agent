import pytest

from trace_id import (
    MAX_TRACE_ID_LEN,
    normalize_trace_id,
    trace_header_name,
    trace_id_from_config,
    trace_id_slug,
)


class TestNormalizeTraceId:
    def test_none_and_blank_mean_absent(self):
        assert normalize_trace_id(None) is None
        assert normalize_trace_id("") is None
        assert normalize_trace_id("   ") is None

    def test_strips_surrounding_whitespace(self):
        assert normalize_trace_id("  abc-123  ") == "abc-123"

    def test_non_string_is_coerced(self):
        assert normalize_trace_id(12345) == "12345"

    def test_rejects_control_characters(self):
        for bad in ("a\nb", "a\rb", "a\tb", "a\x00b", "a\x7fb"):
            with pytest.raises(ValueError, match="управляющие символы"):
                normalize_trace_id(bad)

    def test_rejects_too_long(self):
        with pytest.raises(ValueError, match="длиннее"):
            normalize_trace_id("x" * (MAX_TRACE_ID_LEN + 1))

    def test_accepts_max_length(self):
        value = "x" * MAX_TRACE_ID_LEN
        assert normalize_trace_id(value) == value

    def test_accepts_slashes_and_dots(self):
        # В заголовке эти символы легальны; в путь сырое значение не попадает.
        assert normalize_trace_id("wf/123..v2") == "wf/123..v2"


class TestTraceIdSlug:
    def test_preserves_uuid_shape(self):
        value = "9F2A1C04-1234-4abc-9DEF-000000000001"
        assert trace_id_slug(value) == value

    def test_replaces_unsafe_characters(self):
        assert trace_id_slug("wf/123..v2") == "wf_123__v2"

    def test_truncates_to_64(self):
        assert len(trace_id_slug("y" * 500)) == 64

    def test_non_ascii_is_replaced(self):
        assert trace_id_slug("трейс") == "_____"


class TestTraceIdFromConfig:
    def test_missing_key_is_empty(self):
        assert trace_id_from_config({}) == ""
        assert trace_id_from_config(None) == ""

    def test_empty_string_is_empty(self):
        assert trace_id_from_config({"trace_id": ""}) == ""
        assert trace_id_from_config({"trace_id": "  "}) == ""

    def test_reads_value(self):
        assert trace_id_from_config({"trace_id": " abc "}) == "abc"


class TestTraceHeaderName:
    def test_default_when_absent(self):
        assert trace_header_name({}, "x_trace_id") == "x_trace_id"
        assert trace_header_name({"trace_id_header_name": None}, "trace_id") == "trace_id"
        assert trace_header_name({"trace_id_header_name": "  "}, "trace_id") == "trace_id"

    def test_override(self):
        cfg = {"trace_id_header_name": "X-Trace-Id"}
        assert trace_header_name(cfg, "x_trace_id") == "X-Trace-Id"
        assert trace_header_name(cfg, "trace_id") == "X-Trace-Id"
