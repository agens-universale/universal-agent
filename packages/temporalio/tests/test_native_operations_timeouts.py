"""Лимит activity инструмента следует его timeout, а не 15 минутам."""

from datetime import timedelta

from gc_core.execution.contracts import ToolEffectRequest
from gc_ua_temporalio.native_operations import (
    ACTIVITY_TIMEOUT,
    NativeOperations,
)


def _request(name, **arguments):
    return ToolEffectRequest("op", name, "call", arguments, "digest")


def test_long_tool_timeout_extends_activity_limit():
    operations = NativeOperations(None, "owner", lambda prefix: prefix)
    long = operations._tool_timeout(_request("exec_command", timeout=1800))
    assert long == timedelta(seconds=1800) + timedelta(minutes=5)
    short = operations._tool_timeout(_request("exec_command", timeout=60))
    assert short == ACTIVITY_TIMEOUT
    assert (
        operations._tool_timeout(_request("fs_read_file")) == ACTIVITY_TIMEOUT
    )
    assert (
        operations._tool_timeout(_request("exec_command", timeout="1800"))
        == ACTIVITY_TIMEOUT
    )
