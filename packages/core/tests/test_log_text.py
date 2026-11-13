"""Длинный текст в лог — с началом и концом: обрыв виден в конце."""

from gc_core.utils.log_text import clip_middle


def test_short_text_is_kept_as_is():
    assert clip_middle("abc", 10) == "abc"
    assert clip_middle("a" * 10, 10) == "a" * 10


def test_long_text_keeps_head_tail_and_length():
    text = "H" * 50 + "M" * 100 + "T" * 50
    assert clip_middle(text, 100) == (
        "H" * 50 + " ... [truncated, total_chars=200] ... " + "T" * 50
    )


def test_tiny_limit_still_clips():
    assert clip_middle("abcdef", 1) == " ... [truncated, total_chars=6] ... "
