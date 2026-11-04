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


def _statuses(tmp_path) -> dict:
    store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
    return {t.thread_id: t.status for t in store.list_tasks()}


def test_list_status_comes_from_last_turn_of_runtime(tmp_path):
    """Тред из CLI: в мете исход «completed», короткий итог последнего хода
    (``runtime.last_turn``, его пишет ядро) — остановка лимитом ошибок.
    Строка списка верит итогу хода: «остановлено». Потоков список не
    читает."""
    last_turn = {"status": "stopped", "reason": "tool_errors"}
    _write_meta(
        tmp_path,
        "cli",
        "a",
        {
            "thread_id": "a",
            "status": "completed",
            "runtime": {"last_turn": last_turn},
        },
    )
    _write_meta(
        tmp_path, "cli", "b", {"thread_id": "b", "status": "completed"}
    )
    _write_meta(
        tmp_path,
        "cli",
        "c",
        {
            "thread_id": "c",
            "tui": {"status": "completed"},
            "runtime": {"last_turn": {"status": "error", "reason": None}},
        },
    )

    assert _statuses(tmp_path) == {
        "a": "stopped",
        "b": "completed",
        "c": "failed",
    }


def test_open_turn_wins_over_last_turn(tmp_path):
    """Ход открыт (``runtime.turn``: идёт или оборван без итога): итог
    прошлого хода — не его исход, список — по мете (дальше «прервано»
    решает экран)."""
    last_turn = {"status": "done", "reason": None}
    _write_meta(
        tmp_path,
        "cli",
        "a",
        {
            "thread_id": "a",
            "tui": {"status": "running"},
            "runtime": {"turn": "running", "last_turn": last_turn},
        },
    )
    _write_meta(
        tmp_path,
        "cli",
        "b",
        {
            "thread_id": "b",
            "tui": {"status": "failed"},
            "runtime": {"turn": "interrupted", "last_turn": last_turn},
        },
    )

    assert _statuses(tmp_path) == {"a": "running", "b": "failed"}


def test_pending_question_shows_the_thread_as_asking(tmp_path):
    """Тред приостановлен на вопросе (A2A): ход не закончен — итог
    прошлого хода не его исход, тред ждёт ответа."""
    _write_meta(
        tmp_path,
        "a2a",
        "a",
        {
            "thread_id": "a",
            "pending_question": {"call_id": "ask1", "question": "A или B?"},
            "runtime": {"last_turn": {"status": "done", "reason": None}},
        },
    )
    store = FileTaskStore(threads_dir=str(tmp_path), system="a2a")
    assert {t.thread_id: t.status for t in store.list_tasks()} == {
        "a": "asking"
    }
