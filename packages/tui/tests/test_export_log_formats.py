"""/export log-txt|log-json — выгрузка сырых артефактов сессии."""

from gc_testkit.tui_support import tui_settings
import pytest
from gc_tui.services.stores.file_store import FileTaskStore


def test_read_raw_thread_jsonl_concatenates_archives_then_live(tmp_path):
    (tmp_path / "data_cli_t1__pre_summary__20260101.jsonl").write_text(
        '{"seq": 1, "payload": {"role": "user"}}\n', encoding="utf-8"
    )
    (tmp_path / "data_cli_t1.jsonl").write_text(
        '{"seq": 2, "payload": {"role": "assistant"}}\n', encoding="utf-8"
    )
    store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
    raw = store.read_raw_thread_jsonl("t1")
    assert raw == (
        '{"seq": 1, "payload": {"role": "user"}}\n{"seq": 2, "payload": {"role": "assistant"}}\n'
    )


def test_read_raw_thread_jsonl_no_files_returns_empty(tmp_path):
    store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
    assert store.read_raw_thread_jsonl("nope") == ""


def test_read_raw_thread_jsonl_keeps_leading_summary_lines(tmp_path):
    # В отличие от read_segments лидирующая dialog_summary-строка архива сохраняется.
    (tmp_path / "data_cli_t2__pre_summary__20260101.jsonl").write_text(
        '{"seq": 1, "payload": {"role": "user", "content": "summary"}}\n{"seq": 2, "payload": {"role": "assistant"}}\n',
        encoding="utf-8",
    )
    (tmp_path / "data_cli_t2.jsonl").write_text(
        '{"seq": 3, "payload": {"role": "user"}}\n', encoding="utf-8"
    )
    store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
    raw = store.read_raw_thread_jsonl("t2")
    assert raw.count("\n") == 3
    assert '"content": "summary"' in raw


def _mk_app(tmp_path):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


@pytest.mark.asyncio
async def test_export_log_json_writes_raw_thread(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    (tmp_path / "data_cli_t1.jsonl").write_text(
        '{"seq": 1, "payload": {"role": "user"}}\n', encoding="utf-8"
    )
    app = _mk_app(tmp_path)
    async with app.run_test() as pilot:
        scr = app.screen
        scr._active_task_id = "t1"
        scr._store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
        monkeypatch.chdir(tmp_path)
        scr._cmd_export("log-json")
        await pilot.pause()
    out = list(tmp_path.glob("export-t1-*-log.jsonl"))
    assert len(out) == 1
    assert (
        out[0].read_text(encoding="utf-8")
        == '{"seq": 1, "payload": {"role": "user"}}\n'
    )


@pytest.mark.asyncio
async def test_export_log_txt_copies_log_file(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    log_src = tmp_path / "agent_main_123_abc.log"
    log_src.write_text("log line 1\nlog line 2\n", encoding="utf-8")
    app = _mk_app(tmp_path)
    async with app.run_test() as pilot:
        scr = app.screen
        scr._active_task_id = "t1"
        app._session_log_path = str(log_src)
        monkeypatch.chdir(tmp_path)
        scr._cmd_export("log-txt")
        await pilot.pause()
    out = list(tmp_path.glob("export-t1-*-agent_main_123_abc.log"))
    assert len(out) == 1
    assert out[0].read_text(encoding="utf-8") == "log line 1\nlog line 2\n"


@pytest.mark.asyncio
async def test_export_log_txt_no_log_yet(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    app = _mk_app(tmp_path)
    async with app.run_test() as pilot:
        scr = app.screen
        scr._active_task_id = "t1"
        app._session_log_path = None
        monkeypatch.chdir(tmp_path)
        scr._cmd_export("log-txt")
        await pilot.pause()
    assert list(tmp_path.glob("export-*")) == []


@pytest.mark.asyncio
async def test_export_log_txt_logs_disabled(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    app = _mk_app(tmp_path)
    async with app.run_test() as pilot:
        scr = app.screen
        scr._active_task_id = "t1"
        app._session_log_path = "Null. Логи отключены. Включите логи в файле config.yml если они необходимы"
        monkeypatch.chdir(tmp_path)
        scr._cmd_export("log-txt")
        await pilot.pause()
    assert list(tmp_path.glob("export-*")) == []


@pytest.mark.asyncio
async def test_export_log_txt_file_missing(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    app = _mk_app(tmp_path)
    async with app.run_test() as pilot:
        scr = app.screen
        scr._active_task_id = "t1"
        app._session_log_path = str(tmp_path / "does_not_exist.log")
        monkeypatch.chdir(tmp_path)
        scr._cmd_export("log-txt")
        await pilot.pause()
    assert list(tmp_path.glob("export-*")) == []
