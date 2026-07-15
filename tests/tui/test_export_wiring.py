"""/export [txt|json] — файл из полной истории активного треда."""
import json

import pytest

from thread_store import append_new_thread_messages
from tui.services.stores.file_store import FileTaskStore


def _attach(scr, threads_dir, tid):
    scr._active_task_id = tid
    scr._store = FileTaskStore(threads_dir=str(threads_dir), system="cli")


@pytest.mark.asyncio
async def test_export_default_txt(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    append_new_thread_messages(str(tmp_path), "cli", "tid1",
                               [{"role": "user", "content": "вопрос", "id": "m1"}], set())
    from tui.app import TuiApp
    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        _attach(scr, tmp_path, "tid1")
        # В поле уже `/export ` (после палитры / пробела) — Enter выполняет с дефолтом txt.
        pilot.app.query_one("#cmdinput").text = "/export "
        await pilot.press("enter")
        await pilot.pause()
        files = list(tmp_path.glob("export-tid1-*.txt"))
        assert len(files) == 1
        assert "вопрос" in files[0].read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_export_json(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    append_new_thread_messages(str(tmp_path), "cli", "tid1",
                               [{"role": "user", "content": "вопрос", "id": "m1"}], set())
    from tui.app import TuiApp
    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        _attach(scr, tmp_path, "tid1")
        pilot.app.query_one("#cmdinput").text = "/export json"
        await pilot.press("enter")
        await pilot.pause()
        files = list(tmp_path.glob("export-tid1-*.json"))
        assert len(files) == 1
        doc = json.loads(files[0].read_text(encoding="utf-8"))
        assert doc["segments"][0]["messages"][0]["content"] == "вопрос"


@pytest.mark.asyncio
async def test_export_unknown_fmt_shows_error(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    append_new_thread_messages(str(tmp_path), "cli", "tid1",
                               [{"role": "user", "content": "вопрос", "id": "m1"}], set())
    from tui.app import TuiApp
    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        _attach(scr, tmp_path, "tid1")
        pilot.app.query_one("#cmdinput").text = "/export md"
        await pilot.press("enter")
        await pilot.pause()
        trace = pilot.app.query_one("#trace")
        text = "\n".join(str(s.render()) for s in trace.query("Static"))
        assert "[!]" in text and "md" in text
        assert not list(tmp_path.glob("export-*"))


@pytest.mark.asyncio
async def test_export_no_active_task(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from tui.app import TuiApp
    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = None
        pilot.app.query_one("#cmdinput").text = "/export "
        await pilot.press("enter")
        await pilot.pause()
        trace = pilot.app.query_one("#trace")
        text = "\n".join(str(s.render()) for s in trace.query("Static"))
        assert "[!]" in text and "нет активной задачи" in text
        assert not list(tmp_path.glob("export-*"))
