"""TDD: strip_tagged_blocks / strip_think_blocks — non-greedy closed + unclosed tail."""

from message_projection.projectors.strip_tagged_blocks import (
    strip_tagged_blocks,
    strip_think_blocks,
)


def test_strip_closed_think_keeps_remainder():
    assert strip_think_blocks("<think>думаю</think>Привет!") == "Привет!"


def test_strip_unclosed_think_to_eof():
    assert strip_think_blocks("<think>длинное рассуждение без закрытия") == ""


def test_strip_prefix_before_unclosed_think():
    assert strip_think_blocks("привет <think>думаю дальше") == "привет"


def test_strip_non_greedy_two_closed_blocks_keeps_prose():
    content = "<think>a</think> prose between <think>b</think> end"
    assert strip_think_blocks(content) == "prose between  end"


def test_strip_non_greedy_nested_quoted_first_close():
    # Non-greedy: first <think> → first </think>, prose after may remain dirty.
    content = "<think>рассуждаю про <think>цитата</think> теги и ещё</think>Ответ."
    assert strip_think_blocks(content) == "теги и ещё</think>Ответ."


def test_strip_non_greedy_extra_close_tags_left():
    content = "<think>a</think> b </think> c </think>Финал"
    assert strip_think_blocks(content) == "b </think> c </think>Финал"


def test_strip_empty_and_none_like():
    assert strip_think_blocks("") == ""
    assert strip_think_blocks("   ") == ""


def test_strip_no_tags_unchanged():
    assert strip_think_blocks("просто ответ") == "просто ответ"


def test_strip_only_think_empty():
    assert strip_think_blocks("<think>only</think>") == ""


def test_strip_case_insensitive_tags():
    assert strip_think_blocks("<THINK>x</THINK>Y") == "Y"


def test_strip_tagged_blocks_extra_tag():
    text = "<redacted_thinking>hide</redacted_thinking>keep"
    assert strip_tagged_blocks(text, ("think", "redacted_thinking")) == "keep"


def test_strip_tagged_blocks_think_then_extra():
    text = "<think>t</think> mid <redacted_thinking>r</redacted_thinking> end"
    assert (
        strip_tagged_blocks(text, ("think", "redacted_thinking")) == "mid  end"
    )


def test_strip_think_blocks_equals_tagged_think_only():
    text = "<think>x</think>Y <redacted_thinking>z</redacted_thinking>"
    assert strip_think_blocks(text) == strip_tagged_blocks(text, ("think",))
    assert strip_think_blocks(text) == "Y <redacted_thinking>z</redacted_thinking>"
