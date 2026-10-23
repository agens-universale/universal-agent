"""Разбор вывода команды для превью `Ran` (спека P4 §4.0, §4.1).

`parse_command_output` — конверт exec-тула → `CommandOutput`;
`command_lines` — сырой текст команды построчно, без `cd <корень> &&`.
"""

from __future__ import annotations

import json

from gc_tui.models import CommandOutput
from gc_tui.services.command_output import (
    clean_output_line,
    command_lines,
    omitted_line,
    parse_command_output,
)
from gc_tui.services.trace_mapper import RenderContext, map_message
from gc_tui.services.trace_retention import apply_payload_retention


def _whole(text: str) -> dict:
    return {"content": text, "total_chars": len(text)}


def _envelope(stdout="", stderr="", **head) -> str:
    out = stdout if isinstance(stdout, dict) else _whole(stdout)
    err = stderr if isinstance(stderr, dict) else _whole(stderr)
    envelope = {
        "tool": "exec_command",
        "ok": True,
        "command": "x",
        "exit_code": 0,
        "timed_out": False,
        "error": None,
        **head,
        "stdout": out,
        "stderr": err,
    }
    return json.dumps(envelope, ensure_ascii=False)


def _lines(n: int) -> str:
    return "".join(f"line {i}\n" for i in range(n))


def test_preview_three_of_fourteen():
    parsed = parse_command_output(_envelope(_lines(14)))
    assert parsed.preview == ("line 0", "line 1", "line 2")
    assert parsed.hidden == 11
    assert len(parsed.lines) == 14
    assert parsed.exit_code == 0
    assert parsed.timed_out is False
    assert parsed.background is False


def test_stdout_then_stderr():
    parsed = parse_command_output(_envelope("out\n", "err\n"))
    assert parsed.lines == ("out", "err")


def test_truncated_channel_marker_between_head_and_tail():
    stdout = {
        "truncated": True,
        "head": "a\nb\n",
        "tail": "y\nz\n",
        "total_chars": 1208,
        "omitted_chars": 1200,
    }
    parsed = parse_command_output(_envelope(stdout))
    assert parsed.lines == ("a", "b", "… пропущено 1 200 символов", "y", "z")
    assert parsed.omitted_chars == 1200
    assert parsed.preview == ("a", "b", "y")
    assert parsed.hidden == 1


def test_omitted_line_plural():
    assert omitted_line(1) == "… пропущено 1 символ"
    assert omitted_line(3) == "… пропущено 3 символа"
    assert omitted_line(1200) == "… пропущено 1 200 символов"


def test_exit_code_and_python_return_code():
    assert parse_command_output(_envelope(exit_code=2)).exit_code == 2
    python = _envelope(tool="exec_python", return_code=1)
    data = json.loads(python)
    del data["exit_code"]
    assert parse_command_output(json.dumps(data)).exit_code == 1


def test_timed_out():
    assert parse_command_output(_envelope(timed_out=True)).timed_out is True


def test_background_has_no_lines():
    parsed = parse_command_output(
        _envelope(background={"pid": 7, "output": "not captured"})
    )
    assert parsed.background is True
    assert parsed.lines == ()
    assert parsed.preview == ()
    assert parsed.hidden == 0


def test_empty_output():
    parsed = parse_command_output(_envelope())
    assert parsed.lines == ()
    assert parsed.hidden == 0


def test_ansi_and_carriage_return_cleaned():
    parsed = parse_command_output(_envelope("\x1b[31mFAIL\x1b[0m\r\nok\r\n"))
    assert parsed.lines == ("FAIL", "ok")
    assert clean_output_line("line\r") == "line"
    assert clean_output_line("\x1b[1mbold\x1b[0m") == "bold"


def test_trailing_blank_lines_dropped():
    parsed = parse_command_output(_envelope("a\n\n\n", "\n\n"))
    assert parsed.lines == ("a",)


def test_not_an_exec_envelope():
    assert parse_command_output("plain text") is None
    assert parse_command_output(json.dumps({"ok": True, "lines": 3})) is None
    assert parse_command_output(None) is None


def test_envelope_known_by_tool_key():
    """Конверт узнаётся по ключу `tool`, не по форме: чужой конверт с
    теми же каналами — не вывод команды."""
    other = json.loads(_envelope("x\n"))
    other["tool"] = "exec_fetch_url"
    assert parse_command_output(json.dumps(other)) is None
    no_tool = json.loads(_envelope("x\n"))
    del no_tool["tool"]
    assert parse_command_output(json.dumps(no_tool)) is None


def test_skills_run_script_channels_in_result():
    """У `skills_run_script` каналы и код — внутри `result`."""
    inner = json.loads(_envelope("hello\n", exit_code=3))
    envelope = {
        "tool": "skills_run_script",
        "ok": True,
        "script_ok": False,
        "result": {
            key: inner[key]
            for key in ("exit_code", "timed_out", "stdout", "stderr")
        },
    }
    parsed = parse_command_output(json.dumps(envelope))
    assert parsed.lines == ("hello",)
    assert parsed.exit_code == 3
    failed = {"tool": "skills_run_script", "ok": False, "result": None}
    assert parse_command_output(json.dumps(failed)) is None


def test_command_lines_strip_root_cd_and_split():
    args = json.dumps({"command": "cd /proj && a\nb\n"})
    assert command_lines("exec_command", args, root="/proj") == ("a", "b")


def test_command_lines_single_line():
    args = json.dumps({"command": "a"})
    assert command_lines("exec_command", args, root=None) == ("a",)


def test_command_lines_python_code():
    args = json.dumps({"code": "import json\nprint(1)"})
    assert command_lines("exec_python", args, root=None) == (
        "import json",
        "print(1)",
    )


def test_command_lines_not_a_run_tool():
    args = json.dumps({"path": "a.py"})
    assert command_lines("fs_read_file", args, root=None) == ()
    assert command_lines("exec_command", None, root=None) == ()


def test_mapper_fills_command_fields_before_retention():
    call = map_message(
        {
            "type": "function_call",
            "name": "exec_command",
            "call_id": "x1",
            "arguments": json.dumps({"command": "cd /proj && a\nb"}),
        },
        no=1,
        ts="t",
        context=RenderContext(root="/proj"),
    )
    out = map_message(
        {
            "type": "function_call_output",
            "call_id": "x1",
            "output": _envelope(_lines(5)),
        },
        no=2,
        ts="t",
    )
    apply_payload_retention(call, seg_in_window=False)
    apply_payload_retention(out, seg_in_window=False)
    assert call.full_payload["_retention"] == "light"
    assert call.command_lines == ("a", "b")
    assert isinstance(out.command, CommandOutput)
    assert out.command.hidden == 2


def test_mapper_leaves_other_tools_alone():
    call = map_message(
        {
            "type": "function_call",
            "name": "fs_read_file",
            "call_id": "r1",
            "arguments": json.dumps({"path": "a.py"}),
        },
        no=1,
        ts="t",
    )
    out = map_message(
        {
            "type": "function_call_output",
            "call_id": "r1",
            "output": json.dumps({"ok": True, "content": "x"}),
        },
        no=2,
        ts="t",
    )
    assert call.command_lines == ()
    assert out.command is None
