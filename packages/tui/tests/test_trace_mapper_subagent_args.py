"""subagent_*: сводка вызова — задача субагента от ядра, не пары key=."""

import json

from gc_core.execution.progress import tool_args_summary
from gc_tui.services.trace_mapper import map_message


def _fc(name, args, call_id="c1"):
    return {
        "type": "function_call",
        "name": name,
        "call_id": call_id,
        "arguments": json.dumps(args),
    }


def test_subagent_summary_is_the_task_preview():
    task = "Read the skill layout and report structure findings " * 3
    args = {"reason": "why", "task": task}
    ev = map_message(_fc("subagent_worker", args), no=1, ts="t")
    assert ev.kind == "call"
    assert ev.args_summary == tool_args_summary(
        "subagent_worker", json.dumps(args)
    )
    assert ev.args_summary.startswith("Read the skill layout")
    assert "task=" not in ev.args_summary


def test_subagent_short_task_untruncated():
    ev = map_message(
        _fc(
            "subagent_planner",
            {"reason": "plan phase", "task": "outline steps"},
        ),
        no=1,
        ts="t",
    )
    assert ev.args_summary == "outline steps"
