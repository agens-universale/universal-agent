"""Тесты разметки тела сообщения после суммаризации."""

from summarize.summary_message_body import build_dialog_summary_content


def test_journal_inlined_other_registers_index_only():
    body = build_dialog_summary_content(
        summary_text="S",
        last_dialog_steps="",
        memory_files={
            "journal": "full journal body",
            "arch": "secret arch content\nline2",
            "zeta": "z",
        },
    )
    assert '<file name="journal">' in body
    assert "full journal body" in body
    assert "secret arch content" not in body
    assert "<other_registers>" in body
    assert 'name="arch"' in body
    assert 'chars="25"' in body  # len("secret arch content\\nline2")
    assert 'name="zeta"' in body
    assert 'chars="1"' in body
    assert 'action "read"' in body


def test_only_journal_no_other_registers_block():
    body = build_dialog_summary_content(
        summary_text="S",
        last_dialog_steps="",
        memory_files={"journal": "only j"},
    )
    assert "<other_registers>" not in body
    assert "only j" in body


def test_no_journal_only_named_registers():
    body = build_dialog_summary_content(
        summary_text="S",
        last_dialog_steps="",
        memory_files={"notes": "n1\nn2"},
    )
    assert "<file name=\"journal\">" not in body
    assert "n1" not in body
    assert "other_registers" in body
    assert 'name="notes"' in body


def test_empty_memory_files_skips_section_and_hint():
    body = build_dialog_summary_content(
        summary_text="S",
        last_dialog_steps="tail",
        memory_files={},
    )
    assert "<memory_files>" not in body
    assert "The block above inlines" not in body
    assert "<last_dialog_steps>" in body
    assert "tail" in body


def test_user_intent_log_and_current_sections_with_hints():
    body = build_dialog_summary_content(
        summary_text="S",
        last_dialog_steps="tail",
        memory_files={},
        user_intent_log=["закрытый ход: только план"],
        current_user_intent=["открытый ход: уточнение"],
    )
    assert "<user_intent_log>" in body
    assert "- закрытый ход: только план" in body
    assert "Prior completed user turns" in body
    assert "<current_user_intent>" in body
    assert "- открытый ход: уточнение" in body
    assert "Open user turn" in body
    assert "<user_messages_log>" not in body


def test_empty_current_skips_current_section():
    body = build_dialog_summary_content(
        summary_text="S",
        last_dialog_steps="",
        user_intent_log=["closed only"],
        current_user_intent=[],
    )
    assert "<user_intent_log>" in body
    assert "\n<current_user_intent>\n" not in body
