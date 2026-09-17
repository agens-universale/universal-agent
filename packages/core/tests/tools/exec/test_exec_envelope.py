"""Форма exec-envelope: структурные каналы + front-loaded preview."""

import json

from gc_core.backends.capture import captured_stream_from_text
from gc_core.tools.exec.exec_envelope import (
    build_exec_envelope,
    stream_preview_json,
    stream_to_json,
)

L = 40
P = 3


def _stream(text: str, cap: int = L):
    return captured_stream_from_text(text, cap)


def _envelope(stdout_text: str, stderr_text: str = "", *, p: int = P, cap: int = L) -> dict:
    raw = build_exec_envelope(
        tool="exec_command",
        ok=True,
        exit_code=0,
        timed_out=False,
        stdout=_stream(stdout_text, cap),
        stderr=_stream(stderr_text, cap),
        preview_edge_chars=p,
        head_fields={"command": "cmd", "cwd": "/tmp"},
    )
    return json.loads(raw)


def test_whole_stream_uses_content_and_omits_edges():
    js = stream_to_json(_stream("short output\n"))
    assert js == {"content": "short output\n", "total_chars": 13}
    assert "truncated" not in js
    assert "head" not in js and "tail" not in js and "omitted_chars" not in js


def test_truncated_stream_uses_head_tail_and_omits_content():
    text = "".join(str(i % 10) for i in range(100))
    js = stream_to_json(_stream(text))
    assert js["truncated"] is True
    assert "content" not in js
    assert js["head"] == text[:20]
    assert js["tail"] == text[-20:]
    assert js["total_chars"] == 100
    assert js["omitted_chars"] == 60
    assert len(js["head"]) + len(js["tail"]) + js["omitted_chars"] == js["total_chars"]


def test_truncated_capture_has_no_synthetic_marker():
    js = stream_to_json(_stream("z" * 100))
    assert "omitted" not in js["head"] and "omitted" not in js["tail"]
    assert "..." not in js["head"] and "..." not in js["tail"]


def test_no_preview_for_stream_up_to_two_edges():
    assert stream_preview_json(_stream("y" * (2 * P)), P) is None


def test_preview_appears_one_char_over_two_edges():
    text = "".join(str(i % 10) for i in range(2 * P + 1))
    pv = stream_preview_json(_stream(text), P)
    assert pv == {
        "head": text[:P],
        "tail": text[-P:],
        "total_chars": 2 * P + 1,
        "omitted_chars": 1,
    }


def test_preview_uses_real_edges_of_truncated_stream():
    text = "".join(str(i % 10) for i in range(100))
    pv = stream_preview_json(_stream(text), P)
    assert pv["head"] == text[:P]
    assert pv["tail"] == text[-P:]
    assert pv["omitted_chars"] == 100 - 2 * P


def test_zero_edge_disables_preview():
    assert stream_preview_json(_stream("q" * 500), 0) is None


def test_both_streams_short_have_no_preview_block():
    env = _envelope("short\n", "")
    assert "preview" not in env
    assert "truncated" not in env
    assert env["stdout"]["content"] == "short\n"
    assert env["stderr"]["content"] == ""


def test_short_channel_does_not_appear_in_preview():
    env = _envelope("a" * 100, "tiny")
    assert set(env["preview"]) == {"stdout"}


def test_streams_are_independent_and_budget_is_not_shared():
    env = _envelope("a" * 100, "b" * 100)
    assert env["preview"]["stdout"]["omitted_chars"] == 100 - 2 * P
    assert env["preview"]["stderr"]["omitted_chars"] == 100 - 2 * P
    assert env["stdout"]["omitted_chars"] == 60
    assert env["stderr"]["omitted_chars"] == 60


def test_top_level_truncated_is_or_of_streams():
    assert _envelope("a" * 100, "")["truncated"] is True
    assert _envelope("", "b" * 100)["truncated"] is True
    assert "truncated" not in _envelope("a" * 10, "b" * 10)


def test_preview_without_truncation_does_not_set_truncated():
    """Поток длиннее 2P, но целиком в capture: preview есть, truncated — нет."""
    env = _envelope("a" * 30)
    assert "stdout" in env["preview"]
    assert "truncated" not in env["stdout"]
    assert env["stdout"]["content"] == "a" * 30
    assert "truncated" not in env


def test_preview_precedes_expanded_channels_in_serialized_json():
    raw = build_exec_envelope(
        tool="exec_command",
        ok=False,
        exit_code=1,
        timed_out=False,
        stdout=_stream("a" * 100),
        stderr=_stream("b" * 100),
        preview_edge_chars=P,
        error={"type": "non_zero_exit"},
        head_fields={"command": "cmd", "cwd": "/tmp"},
    )
    assert raw.index('"error"') < raw.index('"preview"') < raw.index('"stdout"')
    assert raw.index('"preview"') < raw.index('"stderr"')


def test_no_stream_output_refs_anywhere():
    raw = build_exec_envelope(
        tool="exec_command",
        ok=True,
        exit_code=0,
        timed_out=False,
        stdout=_stream("a" * 1000),
        stderr=_stream("b" * 1000),
        preview_edge_chars=P,
    )
    assert "output_ref" not in raw
    assert "hint" not in raw


def test_exit_code_key_is_configurable_for_exec_python():
    raw = build_exec_envelope(
        tool="exec_python",
        ok=True,
        exit_code=0,
        exit_code_key="return_code",
        timed_out=False,
        stdout=_stream(""),
        stderr=_stream(""),
        preview_edge_chars=P,
    )
    assert json.loads(raw)["return_code"] == 0
