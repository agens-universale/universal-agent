"""Проводка кнопки ❐: копия наружу; буфер сессии — только с главного экрана."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from gc_tui.messages import CopyBlockRequest
from gc_tui.screens.main import TraceScreen
from gc_tui.screens.preview import PreviewScreen
from textual.app import App, ComposeResult
from gc_core.agent_edits.journal import EditRecord
from gc_tui.widgets.diff_block import DiffBlock


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
            "_push_session_copy_buffer": lambda self, payload: pushed.append(
                payload
            ),
            "cancel_pending_select_copy": lambda self: cancelled.append(True),
        },
    )()
    monkeypatch.setattr(
        "gc_tui.screens.main.copy_out",
        lambda _app, text, **kw: copied.append(text) or True,
        raising=False,
    )
    monkeypatch.setattr(
        type(screen), "app", property(lambda self: app), raising=False
    )

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
            "_push_session_copy_buffer": lambda self, payload: pushed.append(
                payload
            ),
            "cancel_pending_select_copy": lambda self: cancelled.append(True),
        },
    )()
    monkeypatch.setattr(
        "gc_tui.screens.preview.copy_out",
        lambda _app, text, **kw: copied.append(text) or True,
        raising=False,
    )
    monkeypatch.setattr(
        type(screen), "app", property(lambda self: app), raising=False
    )

    screen.on_copy_block_request(CopyBlockRequest("| a |\n|---|\n| 1 |"))

    assert copied == ["| a |\n|---|\n| 1 |"]
    assert pushed == []
    assert cancelled == [True]


_DIFFLIB_HUNKS = "--- \n+++ \n@@ -1,2 +1,2 @@\n-старое\n+новое\n"


class _DiffBlockHost(App):
    """Хост для блока правки — как в тестах DiffBlock."""

    def __init__(self, block: DiffBlock) -> None:
        super().__init__()
        self._block = block
        self.copied: list[str] = []

    def compose(self) -> ComposeResult:
        yield self._block

    def on_copy_block_request(self, event: CopyBlockRequest) -> None:
        self.copied.append(event.payload)


@pytest.mark.asyncio
async def test_diff_block_copy_carries_an_applicable_diff():
    """Копирование блока правок шлёт хэнки С именем файла (Ж5 батча 4) — без
    него `git apply`/`patch` скопированному диффу отказывают."""
    record = EditRecord(
        path="src/app.py",
        turn_id="t1",
        ts=0.0,
        call_id="c1",
        op="edit",
        hunks=_DIFFLIB_HUNKS,
        add=1,
        del_=1,
        before_sha256=None,
        after_sha256=None,
        flags=[],
    )
    block = DiffBlock.for_record(record, event_no=1, collapsed_max_lines=5)
    app = _DiffBlockHost(block)
    async with app.run_test(size=(70, 20)) as pilot:
        await pilot.click(".block-body", times=2)
        await pilot.pause()

    assert len(app.copied) == 1
    assert app.copied[0].startswith("--- a/src/app.py")
