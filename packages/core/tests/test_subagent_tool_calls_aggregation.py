"""Бюджет agent.max_tool_calls — по всему дереву: вызовы сабагента тратят бюджет родителя.

Симметрия с токенами: usage ребёнка уже вливается в родителя инкрементально
(`_push_usage_delta`), счётчик вызовов тулов теперь тоже (`_push_tool_calls_delta`).
"""

from types import SimpleNamespace

from gc_core.tools.subagents.runner import SubagentRunner


def _runner(
    parent_tool_calls: int = 0,
) -> tuple[SubagentRunner, SimpleNamespace]:
    parent = SimpleNamespace(tool_calls=parent_tool_calls)
    runner = SubagentRunner(SimpleNamespace(), parent, {})
    return runner, parent


def test_child_calls_are_added_to_parent():
    runner, parent = _runner(parent_tool_calls=1)  # 1 = сам вызов subagent_*
    runner._push_tool_calls_delta(SimpleNamespace(tool_calls=4))
    assert parent.tool_calls == 5


def test_repeated_push_does_not_double_count():
    """Метод зовётся и на live-tick'ах, и в finally — double-count съел бы бюджет досрочно."""
    runner, parent = _runner()
    child = SimpleNamespace(tool_calls=3)
    runner._push_tool_calls_delta(child)
    runner._push_tool_calls_delta(child)  # тот же снимок — прироста нет
    assert parent.tool_calls == 3
    child.tool_calls = 5  # ребёнок сделал ещё два вызова
    runner._push_tool_calls_delta(child)
    assert parent.tool_calls == 5


def test_no_child_progress_leaves_parent_untouched():
    runner, parent = _runner(parent_tool_calls=7)
    runner._push_tool_calls_delta(SimpleNamespace(tool_calls=0))
    assert parent.tool_calls == 7


def test_non_int_child_counter_is_ignored():
    runner, parent = _runner(parent_tool_calls=2)
    runner._push_tool_calls_delta(SimpleNamespace())  # поля нет вовсе
    runner._push_tool_calls_delta(SimpleNamespace(tool_calls=None))
    assert parent.tool_calls == 2


def test_second_invocation_counts_from_zero_after_reset():
    """Снимок сбрасывается на каждый вызов сабагента (как _usage_pushed), иначе
    второй ребёнок, начинающий с 0, дал бы отрицательную дельту и не посчитался."""
    runner, parent = _runner()
    runner._push_tool_calls_delta(SimpleNamespace(tool_calls=3))
    assert parent.tool_calls == 3
    runner._tool_calls_pushed = 0  # то, что делает run() перед новым ребёнком
    runner._push_tool_calls_delta(SimpleNamespace(tool_calls=2))
    assert parent.tool_calls == 5
