"""Tests for ``tools.exec.truncate_output.truncate_head_tail``.

Спека: docs_md/actual/2026-07-24-exec-output-head-tail-truncate.md (D3).
Бюджет max_chars — на голову+хвост; маркер omitted между ними в бюджет не вжимается.
"""

from tools.exec.truncate_output import MARKER_TEMPLATE, truncate_head_tail


def test_no_truncation_when_within_budget():
    text = "abcdef"
    out = truncate_head_tail(text, max_chars=100)
    assert out.text == text
    assert out.truncated is False
    assert out.original_chars == 6
    assert out.kept_head_chars == 6
    assert out.kept_tail_chars == 0
    assert out.omitted_chars == 0


def test_at_boundary_no_truncation():
    text = "abcdef"
    out = truncate_head_tail(text, max_chars=6)
    assert out.text == text
    assert out.truncated is False
    assert out.original_chars == 6


def test_truncation_keeps_head_and_tail_with_marker():
    text = "0123456789" * 20  # 200 chars
    out = truncate_head_tail(text, max_chars=50)
    assert out.truncated is True
    assert out.original_chars == 200
    assert "[omitted" in out.text and "chars]" in out.text
    assert out.text.startswith("0")
    assert out.text.endswith("9")
    assert out.kept_head_chars + out.kept_tail_chars == 50
    assert out.omitted_chars == 200 - out.kept_head_chars - out.kept_tail_chars
    # Маркер сверху бюджета head+tail.
    assert len(out.text) == 50 + len(MARKER_TEMPLATE.format(omitted=out.omitted_chars))


def test_marker_format_contains_omitted_count():
    text = "A" * 50
    out = truncate_head_tail(text, max_chars=30)
    assert out.truncated is True
    assert f"[omitted {out.omitted_chars} chars]" in out.text


def test_empty_text():
    out = truncate_head_tail("", max_chars=10)
    assert out.text == ""
    assert out.truncated is False
    assert out.original_chars == 0


def test_zero_budget_returns_empty():
    out = truncate_head_tail("abcdef", max_chars=0)
    assert out.text == ""
    assert out.truncated is True
    assert out.original_chars == 6
    assert out.omitted_chars == 6


def test_negative_budget_treated_as_zero():
    out = truncate_head_tail("abcdef", max_chars=-3)
    assert out.text == ""
    assert out.truncated is True
    assert out.original_chars == 6


def test_marker_surrounded_by_newlines():
    text = "0123456789" * 20
    out = truncate_head_tail(text, max_chars=50)
    assert "\n...[omitted" in out.text
    assert "chars]...\n" in out.text


def test_tiny_budget_still_head_tail_marker():
    text = "0123456789"
    out = truncate_head_tail(text, max_chars=3)
    assert out.truncated is True
    assert out.kept_head_chars + out.kept_tail_chars == 3
    assert "[omitted" in out.text


def test_head_tail_split_balanced():
    text = "0123456789ABCDEFGHIJ" * 10  # 200 chars
    out = truncate_head_tail(text, max_chars=40)
    assert out.truncated is True
    assert abs(out.kept_head_chars - out.kept_tail_chars) <= 1
    assert out.kept_head_chars + out.kept_tail_chars == 40


def test_default_budget_is_half_and_half():
    text = "x" * 5000
    out = truncate_head_tail(text, max_chars=4000)
    assert out.truncated is True
    assert out.kept_head_chars == 2000
    assert out.kept_tail_chars == 2000
    assert out.omitted_chars == 1000
    assert out.text.startswith("x" * 2000)
    assert out.text.endswith("x" * 2000)
