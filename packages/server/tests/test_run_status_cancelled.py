"""Снятый прогон отдаётся отдельным статусом, а не как завершённый.

Остановка uvicorn и явная отмена — не отказ агента: клиент обязан их
различать. Статус задачи лежит в хранилище; здесь — его поля и форма ответа
``/runs``.
"""

from types import SimpleNamespace

from gc_core.threads.models import ThreadInfo, ThreadKey
from gc_server.webhook.status import status_body
from gc_server.webhook.surface import _final_fields


def _run(ending, **fields):
    return SimpleNamespace(
        **{"ending": ending, "result": None, "error": None, **fields}
    )


def _record(updated_at=None):
    return SimpleNamespace(
        key=ThreadKey(system="generic", thread="t"),
        info=ThreadInfo(updated_at=updated_at),
    )


def test_cancelled_run_has_its_own_status():
    fields = _final_fields(_run("cancelled", error=RuntimeError("x")))
    assert fields["status"] == "cancelled"
    assert "result" not in fields and "error" not in fields


def test_completed_and_failed_fields():
    done = _final_fields(_run("completed", result="ответ"))
    assert done["status"] == "completed" and done["result"] == "ответ"
    failed = _final_fields(_run("failed", error=RuntimeError("boom")))
    assert failed["status"] == "failed" and "boom" in failed["error"]


def test_status_body_keeps_the_old_shape():
    base = {"system": "generic", "task_id": "t"}
    done = status_body(
        {
            "status": "completed",
            "started_at": 1.0,
            "finished_at": 2.0,
            "result": "ответ",
        },
        _record(),
        base,
    )
    assert done == {
        "status": "completed",
        "system": "generic",
        "task_id": "t",
        "thread_id": "t",
        "started_at": 1.0,
        "finished_at": 2.0,
        "result": "ответ",
    }
    cancelled = status_body(
        {"status": "cancelled", "started_at": 1.0, "finished_at": 2.0},
        _record(),
        base,
    )
    assert "result" not in cancelled and "error" not in cancelled


def test_running_activity_is_the_last_write_into_the_thread():
    running = status_body(
        {"status": "running", "started_at": 1.0},
        _record(updated_at="2026-10-20T10:00:00+00:00"),
        {},
    )
    assert running["status"] == "running"
    assert running["last_activity_at"] > 1.0
