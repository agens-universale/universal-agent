"""Сырые факты исхода вызова для ленты: status и скаляры error."""

import json
from types import SimpleNamespace

from gc_core.execution.progress import (
    QueueSubagentProgressSink,
    emit_progress,
    result_facts,
)


def test_status_taken_as_is():
    out = json.dumps({"ok": False, "status": "step_limit"})
    assert result_facts(out) == ("step_limit", None)


def test_mode_denial_keeps_scalar_error_fields():
    out = json.dumps(
        {
            "ok": False,
            "error": {
                "type": "write_denied_by_mode",
                "mode": "plan",
                "reason": "Запись недоступна в режиме плана",
                "guidance": ["nested", "skipped"],
                "count": 3,
            },
        }
    )
    status, error = result_facts(out)
    assert status is None
    assert error == {
        "type": "write_denied_by_mode",
        "mode": "plan",
        "reason": "Запись недоступна в режиме плана",
    }


def test_long_error_field_is_not_truncated():
    """Факты — как есть: строку `└` из них и из вывода строит одна функция
    потребителя, обрезка ядра развела бы живой вид и возврат в сессию."""
    reason = "слово " * 100 + "\nвторая строка"
    out = {"ok": False, "error": {"type": "action_blocked", "reason": reason}}
    _status, error = result_facts(out)
    assert error == {"type": "action_blocked", "reason": reason}


def test_only_whitelisted_error_keys_are_carried():
    out = {
        "ok": False,
        "error": {
            "type": "permission_denied_by_mode",
            "reason": "r",
            "mode": "plan",
            "message": "m",
            "path": "/p",
            "category": "mcp",
            "guidance": "длинная подсказка модели",
        },
    }
    _status, error = result_facts(out)
    assert error == {
        "type": "permission_denied_by_mode",
        "reason": "r",
        "mode": "plan",
        "message": "m",
        "path": "/p",
    }


def test_success_and_unparsable_give_nothing():
    assert result_facts(json.dumps({"ok": True})) == (None, None)
    assert result_facts("not json") == (None, None)
    assert result_facts(None) == (None, None)


def test_string_error_has_no_fields():
    assert result_facts({"ok": False, "error": "boom"}) == (None, None)


def test_emit_progress_carries_facts_and_defaults_none():
    sink = QueueSubagentProgressSink()
    ctx = SimpleNamespace(
        parent_call_id="p", subagent_progress_sink=sink, run_id="r"
    )
    state = SimpleNamespace(steps=1, max_steps=5)
    emit_progress(
        ctx,
        state,
        "tool_end",
        ok=False,
        result_status="cancelled_by_user",
        result_error={"type": "x"},
    )
    emit_progress(ctx, state, "tool_end", ok=True)
    first, second = sink.drain()
    assert first.result_status == "cancelled_by_user"
    assert first.result_error == {"type": "x"}
    assert second.result_status is None
    assert second.result_error is None
