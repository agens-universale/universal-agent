"""Technical-сообщение агенту после отката: «перечитайте эти файлы».

Тексты достались от прежней механики отката как есть — тесты запирают именно их.
"""
from __future__ import annotations

from gc_core.agent_edits.feedback import (
    build_undo_feedback_body,
    build_undo_feedback_message,
)
from gc_core.message_policy.user_message_policy import is_technical_user_message_text


def test_undo_message_lists_restored_and_deleted():
    msg = build_undo_feedback_message(
        restored_keys=["a.py"],
        deleted_keys=["new.py"],
    )

    assert msg is not None
    assert msg["role"] == "user"
    assert is_technical_user_message_text(msg["content"])
    assert "Changes reverted" in msg["content"]
    assert "- a.py" in msg["content"]
    assert "Deleted previously created files:" in msg["content"]
    assert "- new.py" in msg["content"]
    assert msg["id"]


def test_undo_body_first_sentence_has_no_pending_wording():
    """Первая фраза — без слова похороненной модели (§7 плана волны 4): откат
    возвращает файл к ступени отсчёта, а не к «ожидающим» правкам."""
    body = build_undo_feedback_body(restored_keys=["a.py"])

    assert body is not None
    assert body.startswith("User reverted file changes via Diff-UI undo.")
    assert "pending" not in body.lower()


def test_undo_body_of_restored_only_has_no_deleted_section():
    body = build_undo_feedback_body(restored_keys=["src/foo.py", "src/bar.py"])

    assert body is not None
    assert "src/foo.py" in body and "src/bar.py" in body
    assert "Deleted previously created" not in body


def test_undo_body_of_deleted_only_has_no_restored_section():
    body = build_undo_feedback_body(deleted_keys=["new.py"])

    assert body is not None
    assert "Changes reverted" not in body
    assert "- new.py" in body


def test_undo_message_is_none_without_keys():
    assert build_undo_feedback_message() is None
    assert build_undo_feedback_body() is None
    assert build_undo_feedback_body(restored_keys=[""], deleted_keys=[""]) is None
