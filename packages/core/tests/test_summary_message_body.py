"""Тесты разметки тела сообщения после суммаризации."""

from gc_core.config_modules.intent_scope import (
    DEFAULT_INTENT_LOG_READ_PAGE_RECORD_LIMIT,
)
from gc_core.intent_log import IntentRecord, read_page, serialize, tail_preview
from gc_core.intent_log.operations import utc_now_ts
from gc_core.intent_log.record import ACTION_USER_ASK
from gc_core.summarize.summary_message_body import (
    build_dialog_summary_content,
    build_intent_blocks_prefix,
    format_summary_block,
)
from gc_core.summarize.summary_spec import agent_visible_section_tags
from gc_testkit.helpers.summary_sample import valid_summary_xml_for_tests


def _user_ask(content: str, *, turn: int = 1) -> IntentRecord:
    return IntentRecord(
        turn=turn,
        ts=utc_now_ts(),
        action=ACTION_USER_ASK,
        tool_name=None,
        content=content,
    )


def test_format_summary_block_wraps_plain_text_once():
    assert format_summary_block("inner") == "<summary>\ninner\n</summary>"


def test_format_summary_block_keeps_existing_summary_root():
    xml = valid_summary_xml_for_tests()
    assert format_summary_block(xml) == xml


def test_xml_summary_not_double_wrapped_in_dialog_body():
    xml = valid_summary_xml_for_tests()
    body = build_dialog_summary_content(
        summary_text=xml, last_dialog_steps="tail"
    )
    assert body.count("<summary>") == 1
    assert body.count("</summary>") == 1
    assert any(f"<{tag}>" in body for tag in agent_visible_section_tags())


def test_session_memory_block_inlined():
    body = build_dialog_summary_content(
        summary_text="S",
        last_dialog_steps="",
        session_memory_block=(
            "<session_memory>\n<goal>full goal body</goal>\n<knowledge>secret knowledge</knowledge>\n</session_memory>"
        ),
    )
    assert "<session_memory>" in body
    assert "full goal body" in body
    assert "<knowledge>" in body
    assert "secret knowledge" in body
    assert "<memory_files>" not in body


def test_empty_session_memory_block_skips_section_and_hint():
    body = build_dialog_summary_content(
        summary_text="S",
        last_dialog_steps="tail",
        session_memory_block="",
    )
    assert "<session_memory>" not in body
    assert "session memory tool" not in body.lower()
    assert "<last_dialog_steps>" in body
    assert "tail" in body
    assert "Treat memory and summaries as context" in body
    assert (
        "do not rerun completed experiments unless the user asks or evidence is stale"
        in body
    )


def test_user_intent_log_tail_attrs_and_jsonl_current():
    closed = [
        _user_ask("closed turn one"),
        _user_ask("closed turn two", turn=2),
    ]
    open_turn = [_user_ask("open turn detail")]
    preview = tail_preview(closed, max_chars=10_000)
    current_jsonl = serialize(open_turn)

    body = build_dialog_summary_content(
        summary_text="S",
        last_dialog_steps="tail",
        user_intent_log_preview=preview,
        current_user_intent_jsonl=current_jsonl,
    )

    assert '<user_intent_log total_chars="' in body
    assert 'total_records="2"' in body
    assert 'truncated="false"' in body
    assert preview["content"].strip() in body
    assert "<current_user_intent>" in body
    assert '"action": "user_ask"' in body
    assert "open turn detail" in body
    assert "- closed turn" not in body


def test_empty_closed_log_omits_user_intent_log_block():
    body = build_dialog_summary_content(
        summary_text="S",
        last_dialog_steps="",
        user_intent_log_preview=tail_preview([], max_chars=1000),
        current_user_intent_jsonl=serialize([_user_ask("only open")]),
    )
    assert "<user_intent_log" not in body
    assert "<current_user_intent>" in body


def test_empty_current_skips_current_section():
    closed = [_user_ask("closed only")]
    body = build_dialog_summary_content(
        summary_text="S",
        last_dialog_steps="",
        user_intent_log_preview=tail_preview(closed, max_chars=1000),
        current_user_intent_jsonl=serialize([]),
    )
    assert "<user_intent_log" in body
    assert "<current_user_intent>" not in body


def test_build_intent_blocks_prefix_matches_dialog_summary_wire_format():
    closed = [_user_ask("closed turn"), _user_ask("older", turn=1)]
    open_turn = [_user_ask("open turn")]
    prefix = build_intent_blocks_prefix(
        closed, open_turn, intent_log_preview_chars=10_000
    )
    assert "<user_intent_log " in prefix
    assert '"action": "user_ask"' in prefix
    assert "closed turn" in prefix
    assert "<current_user_intent>" in prefix
    assert "open turn" in prefix
    assert "[intent_scope:raw]" not in prefix
    assert "\n- closed" not in prefix


def test_summary_intent_tail_matches_read_page_slice():
    closed = [_user_ask("x" * 20), _user_ask("y" * 20, turn=2)]
    preview = tail_preview(closed, max_chars=120)
    page = read_page(
        closed,
        preview["offset"],
        preview["limit"],
        default_limit=DEFAULT_INTENT_LOG_READ_PAGE_RECORD_LIMIT,
    )
    assert page["content"] == preview["content"]
    assert preview["truncated"] is True
    body = build_dialog_summary_content(
        summary_text="S",
        last_dialog_steps="",
        user_intent_log_preview=preview,
    )
    assert preview["content"].strip() in body
    assert 'truncated="true"' in body
