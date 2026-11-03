"""Резерв высоты подсказки под выбором: число строк — по самому длинному
варианту при данной ширине, переносом того же движка, что рисует Static."""

from gc_tui.services.hint_height import reserved_lines, wrapped_lines


def test_wrapped_lines_by_words_and_newlines():
    assert wrapped_lines("aaa bbb ccc", 8) == 2
    assert wrapped_lines("aaa bbb ccc\nddd eee fff ggg hhh", 8) == 5


def test_wrapped_lines_empty_or_no_width_is_one_line():
    assert wrapped_lines("", 10) == 1
    assert wrapped_lines("длинный текст подсказки", 0) == 1


def test_reserved_lines_takes_longest_variant():
    assert reserved_lines(["коротко", "aaa bbb ccc ddd", "x"], 8) == 2
    assert reserved_lines([], 8) == 1
