"""/export log-txt|log-json — выгрузка сырых артефактов сессии.

log-json — весь тред (архивы сжатия, затем живая история) строками конверта
develop, собранными из хранилища тредов: строки, которые не читаются как
запись истории, в выгрузку не попадают.
"""

import json

from gc_testkit.tui_support import tui_settings
import pytest
from gc_tui.services.stores.file_store import FileTaskStore


def _line(seq: int, payload: dict) -> str:
    record = {
        "type": "message",
        "seq": seq,
        "ts": f"t{seq}",
        "payload": payload,
    }
    return json.dumps(record, ensure_ascii=False) + "\n"


def _rows(raw: str) -> list[tuple[int, dict]]:
    rows = [json.loads(line) for line in raw.splitlines()]
    assert all(r["type"] == "message" for r in rows)
    return [(r["seq"], r["payload"]) for r in rows]


def test_read_raw_thread_jsonl_concatenates_archives_then_live(tmp_path):
    (tmp_path / "data_cli_t1__pre_summary__20260101.jsonl").write_text(
        _line(1, {"id": "a", "role": "user"}), encoding="utf-8"
    )
    (tmp_path / "data_cli_t1.jsonl").write_text(
        _line(2, {"id": "b", "role": "assistant"}), encoding="utf-8"
    )
    store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
    raw = store.read_raw_thread_jsonl("t1")
    assert _rows(raw) == [
        (1, {"id": "a", "role": "user"}),
        (2, {"id": "b", "role": "assistant"}),
    ]
    assert all(
        (r["system"], r["thread_id"]) == ("cli", "t1")
        for r in map(json.loads, raw.splitlines())
    )


def test_read_raw_thread_jsonl_no_files_returns_empty(tmp_path):
    store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
    assert store.read_raw_thread_jsonl("nope") == ""


def test_read_raw_thread_jsonl_keeps_leading_summary_lines(tmp_path):
    # В отличие от read_segments лидирующая dialog_summary-строка архива сохраняется.
    (tmp_path / "data_cli_t2__pre_summary__20260101.jsonl").write_text(
        _line(1, {"id": "s", "role": "user", "content": "summary"})
        + _line(2, {"id": "a", "role": "assistant"}),
        encoding="utf-8",
    )
    (tmp_path / "data_cli_t2.jsonl").write_text(
        _line(3, {"id": "u", "role": "user"}) + "не json\n", encoding="utf-8"
    )
    store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
    raw = store.read_raw_thread_jsonl("t2")
    assert [seq for seq, _payload in _rows(raw)] == [1, 2, 3]
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
        _line(1, {"id": "a", "role": "user"}), encoding="utf-8"
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
    assert _rows(out[0].read_text(encoding="utf-8")) == [
        (1, {"id": "a", "role": "user"})
    ]


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
