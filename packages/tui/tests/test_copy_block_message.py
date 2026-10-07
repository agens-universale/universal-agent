"""CopyBlockRequest — сообщение «скопировать блок целиком»."""

from __future__ import annotations

from gc_tui.messages import CopyBlockRequest


def test_carries_payload():
    assert CopyBlockRequest("print(1)").payload == "print(1)"
