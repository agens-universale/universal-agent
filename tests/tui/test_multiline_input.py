"""Multi-line input: Ctrl+J / Shift+Enter / Ctrl+Enter / shift+\\r insert newline; Enter submits."""
import pytest


@pytest.mark.asyncio
async def test_ctrl_j_inserts_newline(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.command_input import CommandTextArea

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        ta = pilot.app.query_one("#cmdinput", CommandTextArea)
        ta.focus()
        await pilot.pause()
        ta.text = "line1"
        ta.move_cursor(ta.document.end)
        await pilot.press("ctrl+j")
        await pilot.pause()
        # newline inserted, not submitted
        assert "\n" in ta.text


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["shift+enter", "ctrl+enter", "shift+\r", "ctrl+\r"])
async def test_modified_enter_inserts_newline(tmp_path, key):
    """Shift/Ctrl+Enter (Kitty) и shift/ctrl+\\r (modifyOtherKeys) — перенос, не submit."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.command_input import CommandTextArea

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        ta = pilot.app.query_one("#cmdinput", CommandTextArea)
        ta.focus()
        await pilot.pause()
        ta.text = "line1"
        ta.move_cursor(ta.document.end)
        await pilot.press(key)
        await pilot.pause()
        assert ta.text == "line1\n"


@pytest.mark.asyncio
async def test_enter_submits_multiline(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.command_input import CommandTextArea

    captured = {"run": None}

    class _Fake:
        def run(self, messages, *, thread_id, **kw):
            captured["run"] = messages[0]
            import json
            from pathlib import Path
            (Path(tmp_path) / f"data_cli_{thread_id}.jsonl").write_text(
                json.dumps({"type": "message", "seq": 1, "ts_utc": "t",
                            "payload": {"id": "u1", "role": "user", "content": messages[0]}}) + "\n",
                encoding="utf-8")
            (Path(tmp_path) / f"meta_cli_{thread_id}.json").write_text(
                json.dumps({"thread_id": thread_id, "updated_at": "t", "messages_count": 1}),
                encoding="utf-8")
        def stop_run(self): pass
        def add_message(self, m): pass

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    app._executor_factory = lambda: _Fake()
    async with app.run_test() as pilot:
        ta = pilot.app.query_one("#cmdinput", CommandTextArea)
        ta.text = "first\nsecond"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert captured["run"] == "first\nsecond"
