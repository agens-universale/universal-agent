"""Снятый прогон отдаётся отдельным статусом, а не как завершённый.

Остановка uvicorn и явная отмена — не отказ агента: клиент обязан их
различать.
"""

from types import SimpleNamespace

from gc_server.webhook.surface import run_status_body


def _run(**fields):
    base = dict(
        thread_id="t",
        started_at=1.0,
        finished_at=2.0,
        last_activity_at=2.0,
        done=True,
        cancelled=False,
        error=None,
        result="ответ",
    )
    return SimpleNamespace(**{**base, **fields})


def test_cancelled_run_has_its_own_status():
    body = run_status_body(_run(cancelled=True, result=None), {})
    assert body["status"] == "cancelled"
    assert "result" not in body


def test_cancelled_is_checked_before_error():
    """У снятого прогона может быть и ошибка отмены — статус всё равно
    cancelled, а не failed."""
    body = run_status_body(_run(cancelled=True, error=RuntimeError("x")), {})
    assert body["status"] == "cancelled"


def test_completed_and_failed_and_running():
    assert run_status_body(_run(), {})["result"] == "ответ"
    failed = run_status_body(_run(error=RuntimeError("boom")), {})
    assert failed["status"] == "failed" and "boom" in failed["error"]
    running = run_status_body(_run(done=False), {})
    assert running["status"] == "running"
    assert running["last_activity_at"] == 2.0
