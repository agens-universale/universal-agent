"""Свободный текст без активной задачи → автосоздание UUID-задачи + executor.run."""

from gc_testkit.tui_support import tui_settings
import pytest


@pytest.mark.asyncio
async def test_first_message_creates_task(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    captured = {"run": None}

    class _Fake:
        async def arun(self, messages, *, thread_id, **kw):
            captured["run"] = {"messages": messages, "thread_id": thread_id}
            import json
            from pathlib import Path

            (Path(tmp_path) / f"data_cli_{thread_id}.jsonl").write_text(
                json.dumps(
                    {
                        "type": "message",
                        "seq": 1,
                        "ts_utc": "2026-05-21T12:00:00+00:00",
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
                        "updated_at": "2026-05-21T12:00:00+00:00",
                        "messages_count": 1,
                    }
                ),
                encoding="utf-8",
            )

        def stop_run(self):
            pass

        def add_message(self, m):
            pass

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._executor_factory = lambda: _Fake()

    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "расскажи об этом проекте"
        await pilot.press("enter")
        await pilot.pause(0.3)

    assert captured["run"] is not None
    assert captured["run"]["messages"] == ["расскажи об этом проекте"]
    # thread_id — uuid hex без дефисов (32 chars)
    assert len(captured["run"]["thread_id"]) == 32
    assert app.tui_state.active_task_id == captured["run"]["thread_id"]


@pytest.mark.asyncio
async def test_message_while_running_calls_add_message(tmp_path):
    """Свободный текст при is_running() → executor.add_message, не новый run."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    captured = {"runs": 0, "added": []}

    class _Fake:
        def __init__(self):
            self._evt = None

        async def arun(self, messages, *, thread_id, **kw):
            captured["runs"] += 1
            # симулируем долгий run
            import threading

            self._evt = threading.Event()
            self._evt.wait(timeout=2.0)

        def stop_run(self):
            if self._evt:
                self._evt.set()

        def add_message(self, m):
            captured["added"].append(m)

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._executor_factory = lambda: _Fake()

    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "первое"
        await pilot.press("enter")
        await pilot.pause(0.2)

        assert captured["runs"] == 1
        # Runner running — second message
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "второе сообщение"
        await pilot.press("enter")
        await pilot.pause(0.2)

        assert captured["runs"] == 1  # NO new run
        assert any(
            m.get("content") == "второе сообщение" for m in captured["added"]
        )


@pytest.mark.asyncio
async def test_message_after_stopped_restarts_same_thread(tmp_path):
    """Свободный текст когда runner закончился → новый run, но тот же thread_id."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    captured = {"thread_ids": []}

    class _Fake:
        async def arun(self, messages, *, thread_id, **kw):
            import json
            from pathlib import Path

            captured["thread_ids"].append(thread_id)
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
            # Quick run — no blocking

        def stop_run(self):
            pass

        def add_message(self, m):
            pass

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._executor_factory = lambda: _Fake()

    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "первое"
        await pilot.press("enter")
        await pilot.pause(0.3)

        first_tid = captured["thread_ids"][0]
        # runner finished (synchronous Fake.run)
        # Second message — should restart same thread
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "второе"
        await pilot.press("enter")
        await pilot.pause(0.3)

        assert len(captured["thread_ids"]) == 2
        assert captured["thread_ids"][1] == first_tid  # same thread


@pytest.mark.asyncio
async def test_no_duplicate_user_reply(tmp_path):
    """После ввода — реплика появляется ровно один раз (через polling), не дважды."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.user_reply import UserReplyContainer

    class _Fake:
        async def arun(self, messages, *, thread_id, **kw):
            import json
            import time
            from pathlib import Path

            # attach_active_task + pending mount на UI-потоке — до записи .jsonl.
            time.sleep(0.4)
            (Path(tmp_path) / f"data_cli_{thread_id}.jsonl").write_text(
                json.dumps(
                    {
                        "type": "message",
                        "seq": 1,
                        "ts_utc": "2026-05-21T12:00:00+00:00",
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
                        "updated_at": "2026-05-21T12:00:00+00:00",
                        "messages_count": 1,
                    }
                ),
                encoding="utf-8",
            )

        def stop_run(self):
            pass

        def add_message(self, m):
            pass

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._executor_factory = lambda: _Fake()

    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "привет"
        await pilot.press("enter")
        from gc_testkit.tui_support import wait_until

        def _one_reply():
            return len(pilot.app.query(UserReplyContainer)) == 1

        await wait_until(
            pilot, _one_reply, "ровно одна user-реплика после promote"
        )
        replies = pilot.app.query(UserReplyContainer)
        assert len(replies) == 1
        assert "привет" in str(replies[0].render())
