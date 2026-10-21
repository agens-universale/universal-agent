"""Дедуп по id: запись с уже отрисованным id не рисуется повторно."""

import json

import pytest


def _write(threads_dir, tid, seq, mid, role, content, system="cli"):
    data = threads_dir / f"data_{system}_{tid}.jsonl"
    with open(data, "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "type": "message",
                    "seq": seq,
                    "payload": {"id": mid, "role": role, "content": content},
                }
            )
            + "\n"
        )
    (threads_dir / f"meta_{system}_{tid}.json").write_text(
        json.dumps(
            {
                "thread_id": tid,
                "system_id": system,
                "updated_at": "2026-05-20T10:00:00+00:00",
                "messages_count": seq,
            }
        )
    )


def _trace_text(pilot):
    trace = pilot.app.query_one("#trace")
    return "\n".join(str(c.render()) for c in trace.walk_children())


@pytest.mark.asyncio
async def test_live_tick_skips_already_rendered_id(tmp_path):
    pytest.importorskip("textual")
    _write(tmp_path, "t1", 1, "m1", "user", "первое")
    from gc_tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path), system="cli")
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task("t1")
        await pilot.pause(0.05)

        _write(tmp_path, "t1", 2, "m1", "user", "первое")
        _write(tmp_path, "t1", 3, "m2", "assistant", "второе")
        scr._poll_active()
        await pilot.pause()

        text = _trace_text(pilot)
        assert text.count("первое") == 1
        assert "второе" in text


@pytest.mark.asyncio
async def test_cancel_undo_resend_renders_new_reply(tmp_path):
    pytest.importorskip("textual")
    _write(tmp_path, "t2", 1, "A", "user", "повтор")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.user_reply import UserReplyContainer

    app = TuiApp(threads_dir=str(tmp_path), system="cli")
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task("t2")
        await pilot.pause(0.05)
        widgets = list(scr.query(UserReplyContainer))
        assert len(widgets) == 1

        # Как продакшен-откат: точечно снять виджет + удалить запись по id
        # и сдвинуть курсор; _rendered_ids не очищается.
        widgets[0].remove()
        assert scr._discard_orphan_and_resync("t2", "A")
        await pilot.pause(0.05)
        assert not list(scr.query(UserReplyContainer))

        _write(tmp_path, "t2", 1, "B", "user", "повтор")
        scr._poll_active()
        await pilot.pause(0.1)

        assert _trace_text(pilot).count("повтор") == 1
        assert len(list(scr.query(UserReplyContainer))) == 1
