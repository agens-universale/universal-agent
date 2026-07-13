"""TDD: strip_think_blocks — greedy first <think> → last </think> (or EOF)."""

from message_projection.projectors.strip_think import strip_think_blocks


def test_strip_closed_think_keeps_remainder():
    assert strip_think_blocks("<think>думаю</think>Привет!") == "Привет!"


def test_strip_unclosed_think_to_eof():
    assert strip_think_blocks("<think>длинное рассуждение без закрытия") == ""


def test_strip_prefix_before_unclosed_think():
    assert strip_think_blocks("привет <think>думаю дальше") == "привет"


def test_strip_greedy_nested_quoted_tags():
    content = "<think>рассуждаю про <think>цитата</think> теги и ещё</think>Ответ."
    assert strip_think_blocks(content) == "Ответ."


def test_strip_greedy_extra_close_tags():
    content = "<think>a</think> b </think> c </think>Финал"
    assert strip_think_blocks(content) == "Финал"


def test_strip_empty_and_none_like():
    assert strip_think_blocks("") == ""
    assert strip_think_blocks("   ") == ""


def test_strip_no_tags_unchanged():
    assert strip_think_blocks("просто ответ") == "просто ответ"


def test_strip_only_think_empty():
    assert strip_think_blocks("<think>only</think>") == ""


def test_strip_case_insensitive_tags():
    assert strip_think_blocks("<THINK>x</THINK>Y") == "Y"
