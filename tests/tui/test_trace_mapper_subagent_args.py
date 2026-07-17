"""subagent_* args summary: preview reason/task text, not <N chars>."""
import json

from tui.services.trace_mapper import map_message


def _fc(name, args, call_id="c1"):
    return {
        "type": "function_call",
        "name": name,
        "call_id": call_id,
        "arguments": json.dumps(args),
    }


def test_subagent_long_reason_and_task_show_text_preview():
    reason = "Investigate why the worker hangs on nested tool batches " * 3  # >80
    task = "Read the skill layout and report structure findings " * 2  # >60
    assert len(reason) > 80
    assert len(task) > 60

    ev = map_message(
        _fc("subagent_worker", {"reason": reason, "task": task}),
        no=1,
        ts="t",
    )
    assert ev is not None
    assert ev.kind == "call"
    assert "<chars>" not in (ev.summary or "")
    assert f"<{len(reason)} chars>" not in (ev.summary or "")
    assert f"<{len(task)} chars>" not in (ev.summary or "")
    # превью текста присутствует (начало reason/task)
    assert "Investigate why the worker" in (ev.summary or "")
    assert "Read the skill layout" in (ev.summary or "")
    assert "reason=" in (ev.summary or "")
    assert "task=" in (ev.summary or "")


def test_subagent_short_reason_and_task_untruncated():
    ev = map_message(
        _fc(
            "subagent_planner",
            {"reason": "plan phase", "task": "outline steps"},
        ),
        no=1,
        ts="t",
    )
    assert "reason='plan phase'" in (ev.summary or "")
    assert "task='outline steps'" in (ev.summary or "")


def test_subagent_reason_preview_capped_near_80():
    reason = "R" * 200
    ev = map_message(
        _fc("subagent_critic", {"reason": reason, "task": "ok"}),
        no=1,
        ts="t",
    )
    summary = ev.summary or ""
    # в summary — усечённый reason (~80), не сырые 200 символов подряд
    assert "R" * 200 not in summary
    assert "…" in summary
    # примерно 80 символов превью + маркер
    preview_start = summary.index("reason=")
    preview_chunk = summary[preview_start : preview_start + 120]
    assert preview_chunk.count("R") <= 85


def test_subagent_task_preview_capped_near_60():
    task = "T" * 200
    ev = map_message(
        _fc("subagent_code_reviewer", {"reason": "r", "task": task}),
        no=1,
        ts="t",
    )
    summary = ev.summary or ""
    assert "T" * 200 not in summary
    assert "…" in summary
    preview_start = summary.index("task=")
    preview_chunk = summary[preview_start : preview_start + 100]
    assert preview_chunk.count("T") <= 65


def test_non_subagent_still_uses_chars_placeholder_for_long_strings():
    """Регрессия: generic ветка по-прежнему <N chars> для длинных строк."""
    long_val = "x" * 100
    ev = map_message(
        _fc("session_compact", {"reason": long_val}),
        no=1,
        ts="t",
    )
    assert f"<{len(long_val)} chars>" in (ev.summary or "")
