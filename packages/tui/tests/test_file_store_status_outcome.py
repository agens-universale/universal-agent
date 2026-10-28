"""Исход сессии в списке /sessions: meta.tui.status > верхний meta.status > None.
Регресс-страховка: раньше list_tasks дефолтил в «stopped» и игнорировал tui.status,
из-за чего ВСЕ сессии показывались [stopped]."""

import json
from pathlib import Path

from gc_tui.services.stores.file_store import FileTaskStore


def _write_meta(d: Path, system: str, tid: str, meta: dict) -> None:
    (d / f"meta_{system}_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )


def test_list_status_prefers_tui_then_top_then_none(tmp_path):
    sysid = "cli"
    _write_meta(
        tmp_path,
        sysid,
        "a",
        {"thread_id": "a", "tui": {"status": "completed"}},
    )
    _write_meta(
        tmp_path, sysid, "b", {"thread_id": "b", "status": "running"}
    )  # только верхний
    _write_meta(tmp_path, sysid, "c", {"thread_id": "c"})  # исход не записан
    _write_meta(
        tmp_path,
        sysid,
        "d",
        {"thread_id": "d", "status": "running", "tui": {"status": "stopped"}},
    )  # tui приоритетнее

    store = FileTaskStore(threads_dir=str(tmp_path), system=sysid)
    by_id = {t.thread_id: t.status for t in store.list_tasks()}

    assert by_id == {
        "a": "completed",
        "b": "running",
        "c": None,
        "d": "stopped",
    }


def _write_records(d: Path, tid: str, *records: dict) -> None:
    (d / f"data_cli_{tid}.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
        encoding="utf-8",
    )


def _reply(seq: int, mid: str) -> dict:
    return {
        "type": "message",
        "seq": seq,
        "payload": {"id": mid, "role": "user", "content": "вопрос"},
    }


def _outcome_record(seq: int, opened_by: str, status: str, reason=None):
    from gc_core.persistence.turn_outcome import TurnOutcome

    outcome = TurnOutcome(
        status=status,
        reason=reason,
        opened_by=opened_by,
        started_at="2026-10-22T10:00:00Z",
        ended_at="2026-10-22T10:01:00Z",
        duration_sec=60.0,
        error=None,
    )
    return {
        "type": "turn_outcome",
        "seq": seq,
        "outcome": outcome.to_payload(),
    }


def _row_status(store: FileTaskStore, task) -> str | None:
    return store.turn_status(task.thread_id, task.status)


def test_list_tasks_reads_no_thread_data(tmp_path, monkeypatch):
    """Список — только мета: хвосты data_*.jsonl читает строка списка
    (`turn_status`), а не `list_tasks` (экран зовёт его на каждый символ
    фильтра). Статус списка — исход из меты."""
    from gc_tui.services.stores import file_store

    for tid in ("a", "b", "c"):
        _write_meta(
            tmp_path, "cli", tid, {"thread_id": tid, "status": "completed"}
        )
        _write_records(
            tmp_path,
            tid,
            _reply(1, "u1"),
            _outcome_record(1, "u1", "stopped", reason="tool_errors"),
        )
    reads: list = []
    real = file_store._last_turn_outcome
    monkeypatch.setattr(
        file_store,
        "_last_turn_outcome",
        lambda path: reads.append(path) or real(path),
    )

    tasks = FileTaskStore(threads_dir=str(tmp_path)).list_tasks()

    assert reads == []
    assert [t.status for t in tasks] == ["completed"] * 3


def test_list_status_comes_from_last_turn_outcome_record(tmp_path):
    """Тред из CLI: в мете исход «completed», последняя запись итога —
    остановка лимитом ошибок. Строка списка верит записи хода:
    «остановлено»."""
    _write_meta(
        tmp_path, "cli", "a", {"thread_id": "a", "status": "completed"}
    )
    _write_records(
        tmp_path,
        "a",
        _reply(1, "u1"),
        _outcome_record(1, "u1", "stopped", reason="tool_errors"),
    )
    _write_meta(
        tmp_path, "cli", "b", {"thread_id": "b", "status": "completed"}
    )
    _write_records(tmp_path, "b", _reply(1, "u1"))

    store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
    by_id = {t.thread_id: _row_status(store, t) for t in store.list_tasks()}

    assert by_id == {"a": "stopped", "b": "completed"}


def test_running_meta_wins_over_earlier_turn_record(tmp_path):
    """Ход убит без записи (мета «идёт»), запись итога — от прошлого хода:
    список по-прежнему по мете (дальше «прервано» решает экран)."""
    _write_meta(
        tmp_path,
        "cli",
        "a",
        {"thread_id": "a", "tui": {"status": "running"}},
    )
    _write_records(
        tmp_path,
        "a",
        _reply(1, "u1"),
        _outcome_record(1, "u1", "done"),
        _reply(2, "u2"),
    )

    store = FileTaskStore(threads_dir=str(tmp_path), system="cli")

    assert [_row_status(store, t) for t in store.list_tasks()] == ["running"]


def test_reply_after_last_record_falls_back_to_meta(tmp_path):
    """После последней записи итога начался ход без записи: исход этого
    хода — у меты, не у прошлой записи."""
    _write_meta(
        tmp_path, "cli", "a", {"thread_id": "a", "tui": {"status": "failed"}}
    )
    _write_records(
        tmp_path,
        "a",
        _reply(1, "u1"),
        _outcome_record(1, "u1", "done"),
        _reply(2, "u2"),
    )

    store = FileTaskStore(threads_dir=str(tmp_path), system="cli")

    assert [_row_status(store, t) for t in store.list_tasks()] == ["failed"]
