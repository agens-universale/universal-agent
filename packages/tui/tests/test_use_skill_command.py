"""'/skill X' — нет задачи/idle: pending_skill_id + meta; идёт run: add_message + meta."""

from gc_testkit.tui_support import tui_settings
import json
import threading
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_use_skill_no_active_task_stages(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/skill skill-creator"
        await pilot.press("enter")
        await pilot.pause(0.1)

        assert app.tui_state.pending_skill_id == "skill-creator"


@pytest.mark.asyncio
async def test_use_skill_running_sends_message_and_updates_meta(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    captured: list = []

    class _Fake:
        async def arun(self, messages, *, thread_id, **kw):
            (Path(tmp_path) / f"data_cli_{thread_id}.jsonl").write_text(
                json.dumps(
                    {
                        "type": "message",
                        "seq": 1,
                        "ts_utc": "...",
                        "payload": {
                            "id": "u1",
                            "role": "user",
                            "content": messages[0],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (Path(tmp_path) / f"meta_cli_{thread_id}.json").write_text(
                json.dumps(
                    {
                        "thread_id": thread_id,
                        "updated_at": "...",
                        "messages_count": 1,
                    }
                ),
                encoding="utf-8",
            )
            self._evt = threading.Event()
            self._evt.wait(timeout=3.0)

        def stop_run(self):
            if getattr(self, "_evt", None):
                self._evt.set()

        def add_message(self, m):
            captured.append(m)

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._executor_factory = lambda: _Fake()

    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "поехали"
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert pilot.app.active_runner.is_running()

        inp.text = "/skill skill-creator"
        await pilot.press("enter")
        await pilot.pause(0.2)

        assert any(
            "skill-creator" in str(m.get("content", "")) for m in captured
        )
        tid = app.tui_state.active_task_id
        meta = json.loads(
            (Path(tmp_path) / f"meta_cli_{tid}.json").read_text(
                encoding="utf-8"
            )
        )
        assert "skill-creator" in meta.get("tui", {}).get("skills", [])


@pytest.mark.asyncio
async def test_use_skill_idle_task_stages_pending_no_error(tmp_path):
    """Задача активна, run не идёт (как после /sessions): /skill не ошибка → pending + meta."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "resumed2"
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text(
        json.dumps(
            {
                "type": "message",
                "seq": 1,
                "ts_utc": "2026-05-22T12:00:00+00:00",
                "payload": {"id": "u1", "role": "user", "content": "старое"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps(
            {
                "thread_id": tid,
                "status": "stopped",
                "updated_at": "2026-05-22T12:00:00+00:00",
                "messages_count": 1,
            }
        ),
        encoding="utf-8",
    )

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        await pilot.pause(0.2)
        # Нет live-агента (runner мог быть прогрет на старте через warm, но не запущен).
        assert (
            pilot.app.active_runner is None
            or not pilot.app.active_runner.is_running()
        )

        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/skill skill-creator"
        await pilot.press("enter")
        await pilot.pause(0.2)

        assert app.tui_state.pending_skill_id == "skill-creator"
        meta = json.loads(
            (Path(tmp_path) / f"meta_cli_{tid}.json").read_text(
                encoding="utf-8"
            )
        )
        assert "skill-creator" in meta.get("tui", {}).get("skills", [])
        texts = [
            str(s.render()) for s in pilot.app.screen.query("#trace Static")
        ]
        assert not any("нет активного runner" in t for t in texts)
