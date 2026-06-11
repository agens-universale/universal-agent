"""UserReplyContainer — три состояния (normal/pending/failed): классы и инлайн-маркер."""
import pytest


def test_normal_state_no_failed_marker():
    pytest.importorskip("textual")
    from tui.widgets.user_reply import UserReplyContainer

    w = UserReplyContainer("привет", state="normal")
    rendered = str(w.render())
    assert "[!]" not in rendered
    assert "user-reply-failed" not in w.classes
    assert "user-reply-pending" not in w.classes


def test_pending_state_has_pending_class_no_marker():
    pytest.importorskip("textual")
    from tui.widgets.user_reply import UserReplyContainer

    w = UserReplyContainer("привет", state="pending")
    rendered = str(w.render())
    assert "[!]" not in rendered
    assert "user-reply-pending" in w.classes
    assert "user-reply-failed" not in w.classes


def test_failed_state_has_failed_class_and_inline_marker():
    pytest.importorskip("textual")
    from tui.widgets.user_reply import UserReplyContainer

    w = UserReplyContainer("упало", state="failed")
    rendered = str(w.render())
    assert "[!]" in rendered
    assert "user-reply-failed" in w.classes
    assert "user-reply-pending" not in w.classes


def test_set_state_transitions_pending_to_failed():
    pytest.importorskip("textual")
    from tui.widgets.user_reply import UserReplyContainer

    w = UserReplyContainer("x", state="pending")
    assert "user-reply-pending" in w.classes
    w.set_state("failed")
    assert "user-reply-pending" not in w.classes
    assert "user-reply-failed" in w.classes
    assert "[!]" in str(w.render())


def test_set_state_transitions_pending_to_normal():
    pytest.importorskip("textual")
    from tui.widgets.user_reply import UserReplyContainer

    w = UserReplyContainer("x", state="pending")
    w.set_state("normal")
    assert "user-reply-pending" not in w.classes
    assert "user-reply-failed" not in w.classes
    assert "[!]" not in str(w.render())
