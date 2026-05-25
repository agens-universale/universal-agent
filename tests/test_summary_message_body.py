"""Тесты разметки тела сообщения после суммаризации."""

from summarize.summary_message_body import build_dialog_summary_content


def test_all_nonempty_registers_inlined():
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
    assert '<file name="arch">' in body
    assert "secret arch content" in body
    assert '<file name="zeta">' in body
    assert "z" in body
    assert "<other_registers>" not in body


def test_empty_register_skipped():
    body = build_dialog_summary_content(
        summary_text="S",
        last_dialog_steps="",
        memory_files={"journal": "only j", "empty": "   "},
    )
    assert "only j" in body
    assert '<file name="empty">' not in body


def test_no_journal_only_named_registers_inlined():
    body = build_dialog_summary_content(
        summary_text="S",
        last_dialog_steps="",
        memory_files={"notes": "n1\nn2"},
    )
    assert '<file name="notes">' in body
    assert "n1" in body
    assert "n2" in body


def test_empty_memory_files_skips_section_and_hint():
    body = build_dialog_summary_content(
        summary_text="S",
        last_dialog_steps="tail",
        memory_files={},
    )
    assert "<memory_files>" not in body
    assert "all non-empty thread wiki" not in body
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
