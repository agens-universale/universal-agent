"""Тесты разметки тела сообщения после суммаризации."""

from summarize.summary_message_body import build_dialog_summary_content


def test_working_memory_block_inlined():
    body = build_dialog_summary_content(
        summary_text="S",
        last_dialog_steps="",
        working_memory_block=(
            "<working_memory>\n"
            "<goal>full goal body</goal>\n"
            "<knowledge>secret knowledge</knowledge>\n"
            "</working_memory>"
        ),
    )
    assert "<working_memory>" in body
    assert "full goal body" in body
    assert "<knowledge>" in body
    assert "secret knowledge" in body
    assert "<memory_files>" not in body


def test_empty_working_memory_block_skips_section_and_hint():
    body = build_dialog_summary_content(
        summary_text="S",
        last_dialog_steps="tail",
        working_memory_block="",
    )
    assert "<working_memory>" not in body
    assert "working_memory tool" not in body
    assert "<last_dialog_steps>" in body
    assert "tail" in body


def test_user_intent_log_and_current_sections_with_hints():
    body = build_dialog_summary_content(
        summary_text="S",
        last_dialog_steps="tail",
        user_intent_log=["закрытый ход: только план"],
        current_user_intent=["открытый ход: уточнение"],
    )
    assert "<user_intent_log>" in body
    assert "- закрытый ход: только план" in body
    assert "Prior completed user turns" in body
    assert "<current_user_intent>" in body
    assert "- открытый ход: уточнение" in body
    assert "Open user turn" in body


def test_empty_current_skips_current_section():
    body = build_dialog_summary_content(
        summary_text="S",
        last_dialog_steps="",
        user_intent_log=["closed only"],
        current_user_intent=[],
    )
    assert "<user_intent_log>" in body
    assert "\n<current_user_intent>\n" not in body
