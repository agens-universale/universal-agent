"""Pending-реплика × stream_reset (компакция треда во время бегущего run)."""

from gc_testkit.tui_support import compact_thread, tui_settings
import json
import threading
from pathlib import Path

import pytest


def _read_pending(threads_dir, tid):
    p = Path(threads_dir) / f"meta_cli_{tid}.json"
    if not p.exists():
        return []
    meta = json.loads(p.read_text(encoding="utf-8"))
    return (meta.get("tui") or {}).get("pending_user_messages", [])


@pytest.mark.asyncio
async def test_pending_record_survives_stream_reset(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.user_reply import UserReplyContainer

    tid = "t-compact"
    # Существующий тред (thread_store-формат): 1 user-сообщение + meta.
    with open(tmp_path / f"data_cli_{tid}.jsonl", "w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "type": "message",
                    "thread_id": tid,
                    "system": "cli",
                    "seq": 1,
                    "ts": "2026-06-11T10:00:00+00:00",
                    "payload": {
                        "id": "u1",
                        "role": "user",
                        "content": "ранний вопрос",
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    meta_path = tmp_path / f"meta_cli_{tid}.json"
    meta_path.write_text(
        json.dumps(
            {
                "thread_id": tid,
                "system_id": "cli",
                "updated_at": "2026-06-11T10:00:00+00:00",
                "messages_count": 1,
            }
        ),
        encoding="utf-8",
    )

    class _Fake:
        """Блокируется до stop_run; ничего не пишет (пишем «компакцию» сами)."""

        async def arun(self, messages, *, thread_id, **kw):
            self._evt = threading.Event()
            self._evt.wait(timeout=30.0)

        def stop_run(self):
            if getattr(self, "_evt", None):
                self._evt.set()

        def add_message(self, m):
            pass

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    fake = _Fake()
    app._executor_factory = lambda: fake
    async with app.run_test() as pilot:

        async def wait_until(cond, what):
            for _ in range(200):
                if cond():
                    return
                await pilot.pause(0.05)
            raise AssertionError(f"timeout: {what}")

        screen = pilot.app.screen
        screen.attach_active_task(tid)
        await pilot.pause()

        # Пользователь шлёт сообщение → runner стартует и блокируется;
        # реплика записана как pending в meta, optimistic-виджет смонтирован.
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "не подтверждено"
        await pilot.press("enter")
        await wait_until(
            lambda: any(
                r["content"] == "не подтверждено"
                for r in _read_pending(tmp_path, tid)
            ),
            "pending-запись в meta",
        )
        pending_widgets = [
            w
            for w in pilot.app.query(UserReplyContainer)
            if w._text == "не подтверждено"
        ]
        assert len(pending_widgets) == 1
        assert pending_widgets[0].state == "pending"

        # Компакция ядром: прежняя история — в архив, живая — с нуля;
        # meta-ключи TUI, включая tui.pending_user_messages, сохраняются.
        compact_thread(
            str(tmp_path),
            "cli",
            tid,
            [{"id": "s1", "role": "assistant", "content": "после-сжатия"}],
        )

        def trace_text():
            trace = pilot.app.query_one("#trace")
            return "\n".join(str(c.render()) for c in trace.walk_children())

        await wait_until(
            lambda: "после-сжатия" in trace_text(), "stream_reset-ререндер"
        )

        # I1: GC на stream_reset — truly-orphaned remount failed без re-attach.
        await wait_until(
            lambda: any(
                w._text == "не подтверждено" and w.state == "failed"
                for w in pilot.app.query(UserReplyContainer)
            ),
            "failed pending после stream_reset",
        )
        records = _read_pending(tmp_path, tid)
        assert len(records) == 1
        assert records[0]["content"] == "не подтверждено"
        assert records[0]["status"] == "failed"
        assert screen._pending_user_widgets == {}

        fake.stop_run()
        runner = pilot.app.get_runner()
        await wait_until(lambda: not runner.is_running(), "runner остановился")
