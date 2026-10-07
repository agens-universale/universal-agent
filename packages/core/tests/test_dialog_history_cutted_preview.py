"""Обрезка показа истории: cutted_preview / cutted_envelope (не TEXT CUTTED внутри blob)."""

import html
import re

from gc_core.message_format.dialog_history import (
    format_last_dialog_steps,
    message_text_limited,
    truncate_text,
)
from gc_core.message_format.serialization import message_text_for_summary

_PREVIEW_RE = re.compile(
    r'<cutted_preview shown_chars="(\d+)" original_chars="(\d+)">(.*?)</cutted_preview>',
    re.DOTALL,
)
_ENVELOPE_RE = re.compile(
    r'<cutted_envelope truncated="true" shown_chars="(\d+)" original_chars="(\d+)">(.*?)</cutted_envelope>',
    re.DOTALL,
)


def _unescape(text: str) -> str:
    return html.unescape(text)


def _dump_of(message: dict) -> tuple[str, str]:
    full = message_text_for_summary(message)
    header, _, dump = full.partition("\n")
    return header, dump


def test_short_tool_call_has_no_wrappers_and_no_text_cutted():
    msg = {
        "type": "function_call",
        "name": "fs_write_file",
        "arguments": {"path": "/app/a.py", "content": "ok"},
    }
    text = message_text_limited(msg, 1500)
    assert text == message_text_for_summary(msg)
    assert "cutted_preview" not in text
    assert "cutted_envelope" not in text
    assert "TEXT CUTTED" not in text


def test_long_tool_call_dump_uses_cutted_preview():
    content = "N" * 2000
    msg = {
        "type": "function_call",
        "name": "fs_write_file",
        "arguments": {"path": "/app/a.py", "content": content},
    }
    limit = 1500
    header, dump = _dump_of(msg)
    assert len(dump) > limit
    text = message_text_limited(msg, limit)
    match = _PREVIEW_RE.search(text)
    assert match is not None
    shown, original, inner = (
        int(match.group(1)),
        int(match.group(2)),
        match.group(3),
    )
    prefix = dump[:limit]
    assert shown == limit == len(prefix)
    assert original == len(dump)
    assert _unescape(inner) == prefix
    assert text.startswith(header + "\n")
    assert "TEXT CUTTED" not in text
    assert "Cutted call preview" not in text
    assert "cutted_envelope" not in text


def test_preview_escapes_closing_tag_in_dump():
    poison = "</cutted_preview>" + ("x" * 200)
    msg = {
        "type": "function_call",
        "name": "mcp_any_tool",
        "arguments": poison,
    }
    text = message_text_limited(msg, 80)
    assert text.count("</cutted_preview>") == 1
    match = _PREVIEW_RE.search(text)
    assert match is not None
    escaped_close = html.escape("</cutted_preview>", quote=False)
    assert escaped_close in match.group(3)
    assert "</cutted_preview>" not in match.group(3)
    assert _unescape(match.group(3)).startswith("</cutted_preview>")
    assert "TEXT CUTTED" not in text


def test_user_text_over_limit_uses_envelope_not_text_cutted():
    msg = {"role": "user", "content": "x" * 60000}
    full = message_text_for_summary(msg)
    text = message_text_limited(msg, 50000)
    match = _ENVELOPE_RE.fullmatch(text)
    assert match is not None
    shown, original, inner = (
        int(match.group(1)),
        int(match.group(2)),
        match.group(3),
    )
    assert shown == 50000
    assert original == len(full)
    assert _unescape(inner) == full[:50000]
    assert "TEXT CUTTED" not in text
    assert "cutted_preview" not in text


def test_tool_output_long_dump_uses_preview():
    msg = {
        "type": "function_call_output",
        "output": "o" * 3000,
    }
    text = message_text_limited(msg, 1500)
    header, dump = _dump_of(msg)
    match = _PREVIEW_RE.search(text)
    assert match is not None
    assert int(match.group(1)) == 1500
    assert int(match.group(2)) == len(dump)
    assert _unescape(match.group(3)) == dump[:1500]
    assert text.startswith(header + "\n")
    assert "TEXT CUTTED" not in text


def test_format_last_dialog_steps_long_write_preview():
    msg = {
        "type": "function_call",
        "name": "fs_write_file",
        "arguments": {
            "path": "/app/pystan_analysis.py",
            "content": "RA" + ("z" * 4000),
        },
    }
    steps = format_last_dialog_steps(
        [msg], max_steps=5, message_max_chars=1500
    )
    assert "[function_call] fs_write_file" in steps
    assert "<cutted_preview " in steps
    assert "TEXT CUTTED" not in steps


def test_truncate_text_still_appends_text_cutted():
    assert truncate_text("x" * 10, 4) == "xxxx ... [TEXT CUTTED]"


def test_message_text_limited_no_limit_keeps_full_text():
    msg = {"role": "user", "content": "x" * 100}
    assert message_text_limited(msg, 0) == message_text_for_summary(msg)
