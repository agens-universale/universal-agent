"""Текст мысли для строки ленты: служебные теги рассуждения отдельными
строками (`</thinking>` в конце мысли от модели) не показываются."""

from __future__ import annotations

from gc_tui.services.thought_text import thought_display_text


def test_tail_closing_tags_are_dropped():
    text = "Изучу структуру проекта.\n</thinking>\n</thinking>"
    assert thought_display_text(text) == "Изучу структуру проекта."


def test_tag_lines_dropped_any_case_and_padding():
    text = "  <THINK> \nплан\n\t</Think>\n<thinking>\nитог"
    assert thought_display_text(text) == "план\nитог"


def test_tag_inside_sentence_is_kept():
    text = "Модель пишет </thinking> посреди фразы."
    assert thought_display_text(text) == text


def test_other_tags_are_kept():
    assert thought_display_text("<plan>\nшаг") == "<plan>\nшаг"


def test_only_tags_give_empty_text():
    assert thought_display_text("</thinking>\n</think>") == ""
