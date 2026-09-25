"""Отвеченный вопрос ask_human в трейсе — янтарный '?' + текст, без 'ask_human:'."""

from gc_tui.palette import RUN_COLOR
from gc_tui.services.trace_render import build_answered_question_widget


def test_no_ask_human_prefix_amber_marker():
    w = build_answered_question_widget(
        "Папка вне периметра. Создать там?", event_no=7
    )
    # content — rich.Text: "? <вопрос>" без технического префикса
    assert w._content.plain == "? Папка вне периметра. Создать там?"
    assert "ask_human:" not in w._content.plain
    # маркер '?' янтарный
    assert str(w._content.spans[0].style) == RUN_COLOR


def test_extra_classes_applied():
    w = build_answered_question_widget(
        "Q", event_no=1, extra_classes="ask-human-question hidden"
    )
    assert w.has_class("answered-question")
    assert w.has_class("ask-human-question")
    assert w.has_class("hidden")
    assert w._content.plain == "? Q"
