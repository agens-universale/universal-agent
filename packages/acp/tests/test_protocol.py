"""Разбор строки JSON-RPC и запись одной строки в stdout."""

import json

from gc_acp.protocol import Incoming, StdoutWriter, parse_line


def test_blank_line_is_ignored():
    assert parse_line("   ") is None


def test_request_keeps_id_and_params():
    incoming = parse_line(
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1}}'
    )
    assert incoming == Incoming(
        id=1,
        has_id=True,
        method="initialize",
        params={"protocolVersion": 1},
    )


def test_notification_has_no_id():
    incoming = parse_line(
        '{"jsonrpc":"2.0","method":"session/cancel","params":{"sessionId":"a"}}'
    )
    assert incoming is not None
    assert incoming.has_id is False
    assert incoming.method == "session/cancel"


def test_null_id_is_not_a_request():
    incoming = parse_line('{"id": null, "method": "initialize"}')
    assert incoming is not None
    assert incoming.has_id is False


def test_broken_json_without_id_has_nothing_to_answer():
    incoming = parse_line("not json")
    assert incoming is not None
    assert incoming.parse_error is True
    assert incoming.has_id is False


def test_broken_json_keeps_numeric_id():
    incoming = parse_line('{"id": 7, "method":')
    assert incoming is not None
    assert incoming.parse_error is True
    assert incoming.id == 7


def test_broken_json_keeps_string_id():
    incoming = parse_line('{"id": "abc", "method":')
    assert incoming is not None
    assert incoming.id == "abc"


def test_non_object_is_not_a_request():
    incoming = parse_line("[1, 2]")
    assert incoming is not None
    assert incoming.not_object is True
    assert incoming.has_id is False


def test_object_without_method():
    incoming = parse_line('{"id": 1}')
    assert incoming is not None
    assert incoming.has_id is True
    assert incoming.method is None


async def test_stdout_writer_is_one_line(capsys):
    await StdoutWriter().send({"jsonrpc": "2.0", "id": 1, "result": {"a": 1}})
    out = capsys.readouterr().out
    assert out.endswith("\n")
    assert out.count("\n") == 1
    assert json.loads(out)["result"] == {"a": 1}
