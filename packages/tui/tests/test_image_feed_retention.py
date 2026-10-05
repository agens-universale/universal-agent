"""Картинки в ленте и окно retention ленты (#120): старые сегменты облегчают только
вызовы тулов и их результаты — реплика с картинками и ответ на вопрос агента с
картинками остаются полными и в сегменте вне окна, на главном экране и в превью.
Вложенная реплика субагента под родителем вне окна не монтируется вовсе (правило
retention), под живым — монтируется."""

import json

import pytest

from gc_core.attachments.model import (
    ORIGIN_USER,
    Attachment,
    chip_text,
    user_label,
)
from gc_testkit.tui_support import tui_settings


def _att(n, tmp_path):
    p = tmp_path / f"img{n}.png"
    p.write_bytes(b"\x89PNG")
    return Attachment(
        id=f"{n:016x}",
        kind="image",
        mime="image/png",
        width=10,
        height=10,
        size=4,
        path=str(p),
        origin=ORIGIN_USER,
        label=user_label(n),
        source="clipboard",
        orig_width=10,
        orig_height=10,
    )


def _rec(p):
    return {"ts": "", "payload": p}


def _seg(msgs, *, index, compacted):
    return {
        "index": index,
        "messages": [_rec(m) for m in msgs],
        "compacted_by": (
            {"ts": "", "messages_in_segment": 3, "synthesis": "S"}
            if compacted
            else None
        ),
    }


def _segments(a1, a3):
    old = _seg(
        [
            {
                "id": "u0",
                "role": "user",
                "content": f"{chip_text(1)} старое",
                "attachments": [a1.to_dict()],
            },
            {
                "id": "fc1",
                "type": "function_call",
                "name": "turn_ask_human",
                "call_id": "ah1",
                "arguments": json.dumps({"question": "Q?"}),
            },
            {
                "id": "fo1",
                "type": "function_call_output",
                "call_id": "ah1",
                "output": json.dumps(
                    {
                        "tool": "turn_ask_human",
                        "ok": True,
                        "error": None,
                        "question": "Q?",
                        "response": f"{chip_text(3)} вот",
                    },
                    ensure_ascii=False,
                ),
            },
            {
                "id": "t1",
                "role": "user",
                "content": '<technical type="technical">x</technical>',
                "attachments": [a3.to_dict()],
            },
            {
                "id": "fcs",
                "type": "function_call",
                "name": "subagent_worker",
                "call_id": "sub_old",
                "arguments": json.dumps({"task": "x"}),
            },
            {
                "id": "fos",
                "type": "function_call_output",
                "call_id": "sub_old",
                "output": "{}",
            },
        ],
        index=0,
        compacted=True,
    )
    mid = _seg(
        [{"id": "u1", "role": "user", "content": "mid"}],
        index=1,
        compacted=True,
    )
    live = _seg(
        [
            {"id": "u2", "role": "user", "content": "live"},
            {
                "id": "fcl",
                "type": "function_call",
                "name": "subagent_worker",
                "call_id": "sub_live",
                "arguments": json.dumps({"task": "y"}),
            },
            {
                "id": "fol",
                "type": "function_call_output",
                "call_id": "sub_live",
                "output": "{}",
            },
        ],
        index=2,
        compacted=False,
    )
    return [old, mid, live]


class _Store:
    def __init__(self, records):
        self.records = records

    def read_subagent_trace(self, task_id):
        return self.records


_SIDECAR = [
    {
        "parent_call_id": "sub_old",
        "kind": "user",
        "seq": 1,
        "payload": {"content": "NESTED_OLD"},
    },
    {
        "parent_call_id": "sub_live",
        "kind": "user",
        "seq": 1,
        "payload": {"content": "NESTED_LIVE"},
    },
]


@pytest.mark.asyncio
async def test_main_screen_keeps_images_of_a_segment_outside_the_window(
    tmp_path,
):
    from gc_tui.app import TuiApp
    from gc_tui.widgets.user_reply import NestedUserReply, UserReplyContainer

    a1, a3 = _att(1, tmp_path), _att(3, tmp_path)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._store = _Store(_SIDECAR)
        scr._render_segments(_segments(a1, a3))
        scr._merge_sidecar_nested("t")
        await pilot.pause()
        replies = {r._text: r._images for r in scr.query(UserReplyContainer)}
        assert replies[f"{chip_text(1)} старое"] == (a1,)
        assert replies[f"{chip_text(3)} вот"] == (a3,)
        users = [ev for ev in scr._task_events if ev.kind == "user"]
        assert all(
            ev.full_payload.get("_retention") != "light" for ev in users
        )
        nested = [w._text for w in scr.query(NestedUserReply)]
        assert nested == ["NESTED_LIVE"]


@pytest.mark.asyncio
async def test_preview_keeps_images_of_a_segment_outside_the_window(tmp_path):
    from textual.app import App
    from textual.containers import VerticalScroll

    from gc_tui.services.trace_render import (
        merge_nested_readonly,
        render_segments_readonly,
    )
    from gc_tui.services.trace_retention import subagent_parent_ids_in_window
    from gc_tui.widgets.user_reply import NestedUserReply, UserReplyContainer

    a1, a3 = _att(1, tmp_path), _att(3, tmp_path)
    segs = _segments(a1, a3)

    class _A(App):
        def compose(self):
            yield VerticalScroll(id="t")

        def on_mount(self):
            t = self.query_one("#t", VerticalScroll)
            calls = render_segments_readonly(t, segs)
            merge_nested_readonly(
                t,
                _SIDECAR,
                calls,
                allowed_parent_ids=subagent_parent_ids_in_window(segs),
            )

    async with _A().run_test() as pilot:
        await pilot.pause()
        replies = {
            r._text: r._images
            for r in pilot.app.screen.query(UserReplyContainer)
        }
        assert replies[f"{chip_text(1)} старое"] == (a1,)
        assert replies[f"{chip_text(3)} вот"] == (a3,)
        assert [w._text for w in pilot.app.screen.query(NestedUserReply)] == [
            "NESTED_LIVE"
        ]
