"""UserReplyContainer — три состояния (normal/pending/failed): только смысл
состояния, без классов кромки и инлайн-маркера `[!]` (спека P3 §2.2)."""

import pytest

pytest.importorskip("textual")

from gc_tui.widgets.user_reply import UserReplyContainer

_EDGE_CLASSES = {"user-reply-pending", "user-reply-failed"}


@pytest.mark.parametrize("state", ["normal", "pending", "failed"])
def test_no_bang_and_no_edge_classes_in_any_state(state):
    w = UserReplyContainer("привет", state=state)
    assert w.state == state
    assert "[!]" not in w._band.render().plain
    assert not _EDGE_CLASSES & (set(w.classes) | set(w._band.classes))


def test_set_state_transitions_pending_to_failed_to_normal():
    w = UserReplyContainer("x", state="pending")
    w.set_state("failed")
    assert w.state == "failed"
    w.set_state("normal")
    assert w.state == "normal"
    assert not _EDGE_CLASSES & (set(w.classes) | set(w._band.classes))
