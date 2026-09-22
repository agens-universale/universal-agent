"""Main-screen в состоянии running — header + начало trace."""

import pytest

pytest.importorskip("textual")
pytest.importorskip("pytest_textual_snapshot")


def test_main_running_snapshot(snap_compare, tmp_path):
    from gc_tui.app import TuiApp

    # подготовка: один user message в data, meta — status running
    (tmp_path / "meta_cli_7f3a9b1c.json").write_text(
        '{"thread_id":"7f3a9b1c","status":"running","updated_at":"2026-05-21T12:00:00+00:00",'
        '"messages_count":1,"tui":{"cwd":"/work/project"}}',
        encoding="utf-8",
    )
    (tmp_path / "data_cli_7f3a9b1c.jsonl").write_text(
        '{"type":"message","seq":1,"ts_utc":"2026-05-21T12:00:00+00:00",'
        '"payload":{"id":"u1","role":"user","content":"расскажи про этот проект"}}\n',
        encoding="utf-8",
    )

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "CodeAgent", "backend_api_mode": "chat_completions"}
    )

    async def setup(pilot):
        await pilot.pause()
        pilot.app.screen.attach_active_task("7f3a9b1c")
        await pilot.pause()

    assert snap_compare(app, terminal_size=(120, 32), run_before=setup)
