"""TUI показывает step_limit сабагента как ошибку, а не как успех.

Проверка жила в наборе ЯДРА (subagents/test_step_limit.py) и импортировала
gc_tui.services.trace_mapper. Из-за неё утверждение «cd packages/core && pytest
доказывает самодостаточность ядра» было неверным: набор проходил только потому,
что gc-tui стоит в общем рабочем окружении. Переехала к предмету 18.08.2026.
"""

import json


def test_step_limit_trace_summary_not_success():
    from gc_tui.services.trace_mapper import (
        _looks_like_error,
        _summarize_output,
    )

    out = json.dumps(
        {
            "tool": "subagent_worker",
            "ok": False,
            "status": "step_limit",
            "message": "Subagent hit step limit.",
            "error": {
                "code": "step_limit",
                "message": "Subagent hit step limit.",
                "retryable": True,
            },
            "meta": {"finish_reason": "step_limit", "steps": 50},
        }
    )
    assert _looks_like_error(out) is True
    assert _summarize_output(out, error=True) == "лимит шагов"
