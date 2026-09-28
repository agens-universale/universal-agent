"""Буфер сессии: push_copy_buffer / clamp_copy_buffer (SPEC 2026-07-25 §3.1 B4)."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")


def test_copy_buffer_max_constant():
    from gc_tui.services.copy_buffer import COPY_BUFFER_MAX

    assert COPY_BUFFER_MAX == 20000


def test_push_on_empty_returns_chunk():
    from gc_tui.services.copy_buffer import push_copy_buffer

    assert push_copy_buffer("", "привет") == "привет"


def test_push_prepends_on_top_with_empty_line():
    from gc_tui.services.copy_buffer import push_copy_buffer

    assert push_copy_buffer("первый", "второй") == "второй\n\nпервый"


def test_push_order_newest_on_top():
    from gc_tui.services.copy_buffer import push_copy_buffer

    buf = ""
    buf = push_copy_buffer(buf, "один")
    buf = push_copy_buffer(buf, "два")
    buf = push_copy_buffer(buf, "три")
    assert buf == "три\n\nдва\n\nодин"


def test_push_empty_chunk_no_change():
    from gc_tui.services.copy_buffer import push_copy_buffer

    assert push_copy_buffer("старый", "") == "старый"


def test_push_strips_trailing_newlines_from_chunk():
    from gc_tui.services.copy_buffer import push_copy_buffer

    assert push_copy_buffer("старый", "новый\n\n") == "новый\n\nстарый"


def test_clamp_under_limit_unchanged():
    from gc_tui.services.copy_buffer import clamp_copy_buffer

    assert clamp_copy_buffer("короткий", max_size=100) == "короткий"


def test_clamp_evicts_oldest_chunks_from_bottom():
    from gc_tui.services.copy_buffer import clamp_copy_buffer

    # newest "три" сверху, oldest "один" снизу
    text = "три\n\nдва\n\nодин"
    assert clamp_copy_buffer(text, max_size=9) == "три\n\nдва"


def test_clamp_single_chunk_over_limit_truncated():
    from gc_tui.services.copy_buffer import clamp_copy_buffer

    assert clamp_copy_buffer("x" * 50, max_size=10) == "x" * 10


def test_clamp_first_chunk_over_limit_keeps_truncated_first():
    from gc_tui.services.copy_buffer import clamp_copy_buffer

    text = "x" * 50 + "\n\nмаленький"
    assert clamp_copy_buffer(text, max_size=10) == "x" * 10


def test_push_evicts_when_over_limit():
    from gc_tui.services.copy_buffer import push_copy_buffer

    buf = "старый1\n\nстарый2"  # старый1 новее
    keep = len("новый\n\nстарый1")
    result = push_copy_buffer(buf, "новый", max_size=keep)
    assert result == "новый\n\nстарый1"


def test_push_huge_chunk_replaces_buffer():
    from gc_tui.services.copy_buffer import push_copy_buffer

    big = "y" * 100
    result = push_copy_buffer("старый", big, max_size=10)
    assert result == "y" * 10


def test_clamp_empty_returns_empty():
    from gc_tui.services.copy_buffer import clamp_copy_buffer

    assert clamp_copy_buffer("", max_size=10) == ""
