"""Разбор строки JSON-RPC и запись одной строки в stdout."""

import asyncio
import contextlib
import io
import json
import os
import sys

from gc_acp.protocol import (
    _OVERSIZE_SALVAGE,
    _STDIN_LINE_LIMIT,
    Incoming,
    StdoutWriter,
    parse_line,
    stdin_lines,
)


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
    assert incoming.has_result is False
    assert incoming.has_error is False


def test_response_keeps_result_and_error():
    ok = parse_line(
        '{"jsonrpc":"2.0","id":4,"result":{"outcome":{"outcome":"cancelled"}}}'
    )
    assert ok is not None
    assert ok.method is None
    assert ok.has_id is True
    assert ok.has_result is True
    assert ok.result == {"outcome": {"outcome": "cancelled"}}
    err = parse_line(
        '{"jsonrpc":"2.0","id":5,"error":{"code":-32601,"message":"Method not found"}}'
    )
    assert err is not None
    assert err.has_error is True
    assert err.error == {"code": -32601, "message": "Method not found"}


async def test_stdout_writer_is_one_line(capsys):
    await StdoutWriter().send({"jsonrpc": "2.0", "id": 1, "result": {"a": 1}})
    out = capsys.readouterr().out
    assert out.endswith("\n")
    assert out.count("\n") == 1
    assert json.loads(out)["result"] == {"a": 1}


async def test_stdout_writer_uses_given_stream_not_sys_stdout(capsys):
    stream = io.StringIO()
    await StdoutWriter(stream).send({"jsonrpc": "2.0", "id": 2, "result": {}})
    assert json.loads(stream.getvalue())["id"] == 2
    assert capsys.readouterr().out == ""


class _Stdin:
    def __init__(self, buffer) -> None:
        self.buffer = buffer


def _forbid_connect_read_pipe(monkeypatch) -> None:
    loop = asyncio.get_running_loop()

    def _refused(*_args, **_kwargs):
        raise AssertionError("connect_read_pipe")

    monkeypatch.setattr(loop, "connect_read_pipe", _refused)


def _write_and_close(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        view = view[written:]
    os.close(fd)


async def _lines_from_stdin(monkeypatch, payload: bytes) -> list[str]:
    """Прогнать ``stdin_lines`` через пайп. ``connect_read_pipe`` запрещён."""
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb")
    write_open = True
    monkeypatch.setattr(sys, "stdin", _Stdin(stream))
    _forbid_connect_read_pipe(monkeypatch)
    task = asyncio.create_task(_collect_stdin_lines())
    try:
        await asyncio.sleep(0)
        if task.done():
            return await task
        await asyncio.to_thread(_write_and_close, write_fd, payload)
        write_open = False
        return await asyncio.wait_for(task, timeout=15)
    finally:
        if write_open:
            os.close(write_fd)
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        stream.close()


async def _collect_stdin_lines() -> list[str]:
    return [line async for line in stdin_lines()]


async def test_initialize_line_arrives_from_pipe(monkeypatch):
    raw = b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
    lines = await _lines_from_stdin(monkeypatch, raw)
    incoming = parse_line(lines[0])
    assert incoming is not None
    assert incoming.method == "initialize"
    assert incoming.id == 1


async def test_line_arrives_while_stdin_stays_open(monkeypatch):
    """Редактор пишет строку и не закрывает stdin, пока ждёт ответ."""
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb")
    monkeypatch.setattr(sys, "stdin", _Stdin(stream))
    _forbid_connect_read_pipe(monkeypatch)
    agen = stdin_lines()
    first = asyncio.create_task(anext(agen))
    second: asyncio.Task | None = None
    try:
        await asyncio.sleep(0)
        if first.done():
            await first
        os.write(
            write_fd,
            b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n',
        )
        line = await asyncio.wait_for(first, timeout=1)
        assert line == (
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
        )
        second = asyncio.create_task(anext(agen))
        await asyncio.sleep(0.05)
        assert not second.done()
        os.write(write_fd, b'{"id":2}\n')
        assert await asyncio.wait_for(second, timeout=1) == '{"id":2}'
    finally:
        os.close(write_fd)
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        # Поток ещё может стоять в read. Закрытый write-конец будит его,
        # и только потом можно закрывать сам поток.
        await asyncio.sleep(0.05)
        await agen.aclose()
        stream.close()


async def test_crlf_matches_lf(monkeypatch):
    lf = await _lines_from_stdin(monkeypatch, b'{"id":1}\n')
    crlf = await _lines_from_stdin(monkeypatch, b'{"id":1}\r\n')
    assert lf == crlf == ['{"id":1}']


async def test_blank_line_is_yielded(monkeypatch):
    assert await _lines_from_stdin(monkeypatch, b"\n") == [""]


async def test_tail_without_newline_is_a_line(monkeypatch):
    assert await _lines_from_stdin(monkeypatch, b'{"id":3}') == ['{"id":3}']


async def test_empty_stdin_yields_nothing(monkeypatch):
    assert await _lines_from_stdin(monkeypatch, b"") == []


async def test_prompt_over_default_64k_arrives_whole(monkeypatch):
    payload = (
        b'{"jsonrpc":"2.0","id":1,"method":"session/prompt","params":{"text":"'
        + b"x" * 70_000
        + b'"}}\n'
    )
    lines = await _lines_from_stdin(monkeypatch, payload)
    assert len(lines) == 1
    assert len(lines[0]) > 64 * 1024
    assert json.loads(lines[0])["id"] == 1


async def test_line_at_limit_arrives_whole(monkeypatch):
    prefix = b'{"id":1,"x":"'
    suffix = b'"}'
    pad = _STDIN_LINE_LIMIT - len(prefix) - len(suffix)
    payload = prefix + b"z" * pad + suffix + b"\n"
    lines = await _lines_from_stdin(monkeypatch, payload)
    assert len(lines) == 1
    assert len(lines[0].encode()) == _STDIN_LINE_LIMIT
    assert json.loads(lines[0])["id"] == 1


async def test_line_over_limit_keeps_prefix_and_next_line(monkeypatch):
    head = b'{"id":7,"x":"'
    extra = _STDIN_LINE_LIMIT - len(head) + 8
    payload = head + b"y" * extra + b'"}\n{"id":2,"method":"ping"}\n'
    lines = await _lines_from_stdin(monkeypatch, payload)
    assert len(lines) == 2
    assert len(lines[0].encode()) == _OVERSIZE_SALVAGE
    broken = parse_line(lines[0])
    assert broken is not None
    assert broken.parse_error is True
    assert broken.id == 7
    nxt = parse_line(lines[1])
    assert nxt == Incoming(id=2, has_id=True, method="ping")


async def test_bad_utf8_skips_line_and_continues(monkeypatch, capsys):
    payload = b"\xff\n" + b'{"id":1}\n'
    assert await _lines_from_stdin(monkeypatch, payload) == ['{"id":1}']
    assert capsys.readouterr().err.splitlines() == ["gc-acp: parse error"]


async def test_oserror_ends_stdin_once(monkeypatch, capsys):
    class _Buffer:
        calls = 0

        def read(self, _n=-1):
            type(self).calls += 1
            raise OSError(6, "Bad file descriptor")

    monkeypatch.setattr(sys, "stdin", _Stdin(_Buffer()))
    _forbid_connect_read_pipe(monkeypatch)
    assert [line async for line in stdin_lines()] == []
    assert _Buffer.calls == 1
    assert capsys.readouterr().err.splitlines() == ["gc-acp: stdin read error"]


async def test_stdin_read_keeps_event_loop_policy(monkeypatch):
    policy = asyncio.get_event_loop_policy()
    seen = []
    monkeypatch.setattr(
        asyncio,
        "set_event_loop_policy",
        lambda *args, **_kwargs: seen.append(args),
    )
    assert await _lines_from_stdin(monkeypatch, b"") == []
    assert seen == []
    assert asyncio.get_event_loop_policy() is policy


async def test_loop_stays_free_while_stdin_blocks(monkeypatch):
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb")
    monkeypatch.setattr(sys, "stdin", _Stdin(stream))
    _forbid_connect_read_pipe(monkeypatch)
    task = asyncio.create_task(_collect_stdin_lines())
    write_open = True
    try:
        await asyncio.sleep(0)
        if task.done():
            await task
        await asyncio.wait_for(asyncio.sleep(0.05), timeout=1)
        assert not task.done()
        await asyncio.to_thread(_write_and_close, write_fd, b"ping\n")
        write_open = False
        assert await asyncio.wait_for(task, timeout=2) == ["ping"]
    finally:
        if write_open:
            os.close(write_fd)
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        stream.close()


async def test_serve_reads_tail_and_shuts_down(monkeypatch):
    seen = {"lines": [], "down": False}

    class _Facade:
        async def handle_line(self, line: str) -> None:
            seen["lines"].append(line)

        async def shutdown(self) -> None:
            seen["down"] = True

    monkeypatch.setattr(
        "gc_acp.main.build_facade", lambda *_args, **_kwargs: _Facade()
    )
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb")
    monkeypatch.setattr(sys, "stdin", _Stdin(stream))
    _forbid_connect_read_pipe(monkeypatch)
    from gc_acp.main import serve

    task = asyncio.create_task(serve(None, io.StringIO()))
    write_open = True
    try:
        await asyncio.sleep(0)
        if task.done():
            await task
        await asyncio.to_thread(_write_and_close, write_fd, b'{"id":9}')
        write_open = False
        await asyncio.wait_for(task, timeout=5)
    finally:
        if write_open:
            os.close(write_fd)
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        stream.close()
    assert seen["lines"] == ['{"id":9}']
    assert seen["down"] is True
