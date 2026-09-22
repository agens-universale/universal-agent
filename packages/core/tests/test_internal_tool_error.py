"""Spec: apply_tool internal exceptions → slim JSON, no traceback in tool output."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from gc_core.tools import (
    INTERNAL_TOOL_ERROR,
    WrongFunctionArgumentsException,
    aapply_tool,
    format_internal_tool_error_output,
    is_internal_tool_error_output,
    is_unknown_tool_output,
)


def _tools_data(name: str, fabric, *, injected_fields=None, properties=None, required=None):
    props = properties if properties is not None else {}
    req = required if required is not None else list(props.keys())
    return {
        name: {
            "tool_fabric": fabric,
            "spec": {
                "type": "function",
                "name": name,
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": req,
                    "additionalProperties": False,
                },
            },
            "description": "",
            "injected_fields": list(injected_fields or []),
        }
    }


class TestFormatInternalToolErrorOutput:
    def test_shape_and_no_stack_fields(self):
        raw = format_internal_tool_error_output(
            tool_name="fs_find_in_files",
            exc=TypeError("got an unexpected keyword argument 'reason'"),
            call_id="call_abc",
        )
        payload = json.loads(raw)
        assert payload == {
            "ok": False,
            "error": INTERNAL_TOOL_ERROR,
            "exception_type": "TypeError",
            "message": "got an unexpected keyword argument 'reason'",
            "guidance": payload["guidance"],
            "tool": "fs_find_in_files",
            "call_id": "call_abc",
        }
        assert "arguments-schema rejection" in payload["guidance"]
        assert "identical call" in payload["guidance"]
        for banned in (
            "traceback",
            "stack",
            "stacktrace",
            "exc_info",
            "Traceback",
            'File "',
            "Agent tool execution Error",
        ):
            assert banned not in raw

    def test_omits_empty_tool_and_call_id(self):
        payload = json.loads(format_internal_tool_error_output(tool_name="", exc=RuntimeError("x")))
        assert "tool" not in payload
        assert "call_id" not in payload
        assert payload["message"] == "x"
        assert payload["exception_type"] == "RuntimeError"

    def test_empty_str_exc_falls_back_to_type_name(self):
        payload = json.loads(format_internal_tool_error_output(tool_name="t", exc=RuntimeError()))
        assert payload["message"] == "RuntimeError"

    def test_message_truncated(self):
        long = "x" * 600
        payload = json.loads(format_internal_tool_error_output(tool_name="t", exc=ValueError(long)))
        assert len(payload["message"]) == 500
        assert payload["message"].endswith("...")


class TestIsInternalToolErrorOutput:
    def test_positive(self):
        raw = format_internal_tool_error_output(tool_name="t", exc=TypeError("boom"))
        assert is_internal_tool_error_output(raw) is True
        assert is_unknown_tool_output(raw) is False

    def test_negative_unknown_and_domain(self):
        assert (
            is_internal_tool_error_output(
                json.dumps(
                    {
                        "ok": False,
                        "error": {"type": "unknown_tool", "message": "x"},
                        "message": "x",
                    }
                )
            )
            is False
        )
        assert (
            is_internal_tool_error_output(
                json.dumps(
                    {
                        "ok": False,
                        "error": {"type": "invalid_argument", "message": "bad"},
                    }
                )
            )
            is False
        )
        assert is_internal_tool_error_output("not json") is False
        assert is_internal_tool_error_output("") is False


class TestApplyToolInternalError:
    async def test_exception_becomes_slim_json_no_traceback(self, caplog):
        class BoomTool:
            def __init__(self, **_kw):
                pass

            async def acall(self, **_kw):
                raise TypeError("got an unexpected keyword argument 'reason'")

        tools = _tools_data(
            "fs_find_in_files",
            BoomTool,
            properties={
                "path": {"type": "string"},
                "pattern": {"type": "string"},
            },
            required=["path", "pattern"],
        )
        logger = logging.getLogger("test_internal_tool_error.apply")
        ctx = SimpleNamespace(
            tools_data=tools,
            config_dict={},
            logger=logger,
        )

        with caplog.at_level(logging.ERROR, logger=logger.name):
            raw = await aapply_tool(
                tool_name="fs_find_in_files",
                arguments=json.dumps({"path": "/tmp/x", "pattern": "def test_"}),
                ctx=ctx,
                call_id="b4zQxElPLq4I0jqTKnW5UeTWH5URyawf",
            )

        payload = json.loads(raw)
        assert payload["ok"] is False
        assert payload["error"] == INTERNAL_TOOL_ERROR
        assert payload["tool"] == "fs_find_in_files"
        assert payload["exception_type"] == "TypeError"
        assert "reason" in payload["message"]
        assert payload["call_id"] == "b4zQxElPLq4I0jqTKnW5UeTWH5URyawf"
        assert payload["guidance"]
        assert "Traceback (most recent call last)" not in raw
        assert 'File "' not in raw
        assert "Agent tool execution Error" not in raw
        assert is_internal_tool_error_output(raw) is True

        assert any(
            "apply_tool internal error" in rec.getMessage() and rec.exc_info is not None for rec in caplog.records
        )

    async def test_async_entry_gives_the_same_slim_envelope(self, caplog):
        """У асинхронного входа контракт обязан быть тот же, что у синхронного.

        Свойство несущее и ломается тихо: тул падает, агент получает текст, всё
        выглядит рабочим — а модель вместо конверта видит `str(exc)` с полным
        traceback. Именно так `aapply_tool` и жил после вливания веток, потому
        что проверка была только на синхронный вход.
        """

        class BoomTool:
            def __init__(self, **_kw):
                pass

            async def acall(self, **_kw):
                raise TypeError("got an unexpected keyword argument 'reason'")

        tools = _tools_data(
            "fs_find_in_files",
            BoomTool,
            properties={
                "path": {"type": "string"},
                "pattern": {"type": "string"},
            },
            required=["path", "pattern"],
        )
        logger = logging.getLogger("test_internal_tool_error.aapply")
        ctx = SimpleNamespace(tools_data=tools, config_dict={}, logger=logger)

        with caplog.at_level(logging.ERROR, logger=logger.name):
            raw = await aapply_tool(
                tool_name="fs_find_in_files",
                arguments=json.dumps({"path": "/tmp/x", "pattern": "def test_"}),
                ctx=ctx,
                call_id="b4zQxElPLq4I0jqTKnW5UeTWH5URyawf",
            )

        payload = json.loads(raw)
        assert payload["ok"] is False
        assert payload["error"] == INTERNAL_TOOL_ERROR
        assert payload["tool"] == "fs_find_in_files"
        assert payload["exception_type"] == "TypeError"
        assert payload["call_id"] == "b4zQxElPLq4I0jqTKnW5UeTWH5URyawf"
        assert "Traceback (most recent call last)" not in raw
        assert "Agent tool execution Error" not in raw
        assert is_internal_tool_error_output(raw) is True

        assert any(
            "apply_tool internal error" in rec.getMessage() and rec.exc_info is not None for rec in caplog.records
        )

    async def test_domain_ok_false_return_not_rewrapped(self):
        domain = {
            "tool": "fs_find_in_files",
            "ok": False,
            "error": {
                "type": "invalid_argument",
                "field": "query_mode",
                "message": "bad mode",
            },
            "matches": [],
            "count": 0,
        }

        class DomainTool:
            def __init__(self, **_kw):
                pass

            async def acall(self, **_kw):
                return domain

        tools = _tools_data("fs_find_in_files", DomainTool, properties={})
        raw = await aapply_tool(
            tool_name="fs_find_in_files",
            arguments="{}",
            tools_data_=tools,
        )
        assert json.loads(raw) == domain
        assert is_internal_tool_error_output(raw) is False

    async def test_wrong_arguments_still_raises(self):
        class NeverCalled:
            def __init__(self, **_kw):
                raise AssertionError("fabric must not run on bad args")

            async def acall(self, **_kw):
                raise AssertionError("call must not run on bad args")

        tools = _tools_data(
            "fs_write_file",
            NeverCalled,
            properties={
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            required=["path", "content"],
        )
        with pytest.raises(WrongFunctionArgumentsException):
            await aapply_tool(
                tool_name="fs_write_file",
                arguments=json.dumps({"path": "/tmp/x"}),
                tools_data_=tools,
            )

    async def test_unknown_tool_detector_unaffected(self):
        raw = await aapply_tool(
            tool_name="no_such_tool_zzz",
            arguments="{}",
            tools_data_=_tools_data(
                "session_think",
                lambda **_kw: MagicMock(__call__=lambda **k: "ok"),
            ),
        )
        assert is_unknown_tool_output(raw) is True
        assert is_internal_tool_error_output(raw) is False
