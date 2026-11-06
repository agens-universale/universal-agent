"""Повтор служебного моста не показывается и ответом не считается."""

from __future__ import annotations

import pytest

from gc_tui.services.export import agent_answers
from gc_tui.services.stream_sink import format_stream_block
from gc_tui.services.trace_mapper import map_message

BRIDGE = "(tool results received)"
MENTION = f"Модель вернула {BRIDGE}, это служебный текст моста."


def _assistant(text, mid):
    return {"role": "assistant", "content": text, "id": mid}


def _segment(payloads):
    return {
        "index": 0,
        "messages": [{"payload": p, "ts": "t"} for p in payloads],
        "compacted_by": None,
    }


def _env(payload):
    return {"type": "message", "ts": "t", "seq": 1, "payload": payload}


@pytest.mark.parametrize(
    "text", [BRIDGE, f"  {BRIDGE}\n", "Tool results received."]
)
def test_mapper_drops_bridge_echo(text):
    assert map_message(_assistant(text, "a"), no=1, ts="t") is None


def test_mapper_keeps_answer_mentioning_bridge():
    ev = map_message(_assistant(MENTION, "a"), no=1, ts="t")
    assert (ev.kind, ev.summary) == ("agent", MENTION)


def test_agent_answers_exclude_echo_keep_mention():
    payloads = [
        {"role": "user", "content": "вопрос"},
        _assistant(BRIDGE, "a1"),
        _assistant(MENTION, "a2"),
    ]
    assert agent_answers([_segment(payloads)]) == [MENTION]


def test_stream_block_hides_echo_keeps_mention():
    assert format_stream_block(answer=BRIDGE) == ""
    assert format_stream_block(answer=MENTION) == MENTION


@pytest.mark.asyncio
async def test_preview_and_rebuild_mount_no_row_for_echo():
    from gc_tui.services.trace_render import render_segments_readonly
    from gc_tui.widgets.trace_row import TraceRow
    from textual.app import App
    from textual.containers import VerticalScroll

    segments = [
        {
            "index": 0,
            "messages": [
                _env({"role": "user", "content": "вопрос", "id": "u"}),
                _env(_assistant(BRIDGE, "a1")),
                _env(_assistant(MENTION, "a2")),
            ],
            "compacted_by": None,
        }
    ]

    class _App(App):
        def compose(self):
            yield VerticalScroll(id="v")

        def on_mount(self):
            render_segments_readonly(self.query_one("#v"), segments)

    async with _App().run_test() as pilot:
        await pilot.pause()
        scr = pilot.app.screen
        body = "\n".join(str(w.render()) for w in scr.query())
    assert BRIDGE not in body.replace(MENTION, "")
    assert MENTION in body
