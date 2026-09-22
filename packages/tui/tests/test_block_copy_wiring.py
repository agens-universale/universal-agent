"""Проводка кнопки ❐: копия наружу; буфер сессии — только с главного экрана."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from gc_tui.messages import CopyBlockRequest
from gc_tui.screens.main import TraceScreen
from gc_tui.screens.preview import PreviewScreen


def test_main_screen_copies_and_pushes_session_buffer(monkeypatch):
    copied: list[str] = []
    pushed: list[str] = []
    cancelled: list[bool] = []

    screen = TraceScreen.__new__(TraceScreen)
    app = type(
        "_App",
        (),
        {
            "copy_to_clipboard": lambda self, text: None,
            "run_worker": None,
            "_push_session_copy_buffer": lambda self, payload: pushed.append(payload),
            "cancel_pending_select_copy": lambda self: cancelled.append(True),
        },
    )()
    monkeypatch.setattr(
        "gc_tui.screens.main.copy_out", lambda _app, text, **kw: copied.append(text) or True, raising=False
    )
    monkeypatch.setattr(type(screen), "app", property(lambda self: app), raising=False)

    screen.on_copy_block_request(CopyBlockRequest("print(1)"))

    assert copied == ["print(1)"]
    assert pushed == ["print(1)"]
    assert cancelled == [True]


def test_preview_screen_copies_without_session_buffer(monkeypatch):
    copied: list[str] = []
    pushed: list[str] = []
    cancelled: list[bool] = []

    screen = PreviewScreen.__new__(PreviewScreen)
    app = type(
        "_App",
        (),
        {
            "copy_to_clipboard": lambda self, text: None,
            "_push_session_copy_buffer": lambda self, payload: pushed.append(payload),
            "cancel_pending_select_copy": lambda self: cancelled.append(True),
        },
    )()
    monkeypatch.setattr(
        "gc_tui.screens.preview.copy_out", lambda _app, text, **kw: copied.append(text) or True, raising=False
    )
    monkeypatch.setattr(type(screen), "app", property(lambda self: app), raising=False)

    screen.on_copy_block_request(CopyBlockRequest("| a |\n|---|\n| 1 |"))

    assert copied == ["| a |\n|---|\n| 1 |"]
    assert pushed == []
    assert cancelled == [True]
