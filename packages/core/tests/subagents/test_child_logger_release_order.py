"""Логгер субагента освобождается ПОСЛЕ записи о его завершении.

`release_run_logger` стоял во внутреннем `finally`, а `_log_lifecycle(
event="subagent_end")` шёл после него — запись о завершении в логе субагента
терялась. Сторож структурный: освобождение — только во внешнем `finally`, а
записей о завершении там нет.
"""

import ast
import inspect
import textwrap

from gc_core.tools.subagents.runner import SubagentRunner


def _is_release(node) -> bool:
    return (
        isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "release_run_logger"
    )


def _is_end_record(node) -> bool:
    return isinstance(node, ast.Call) and any(
        isinstance(k.value, ast.Constant) and k.value.value == "subagent_end"
        for k in node.keywords
    )


def test_logger_is_released_after_every_subagent_end_record():
    fn = ast.parse(
        textwrap.dedent(inspect.getsource(SubagentRunner._arun_internal))
    ).body[0]
    outer = [st for st in fn.body if isinstance(st, ast.Try)][-1]
    in_final = {id(n) for st in outer.finalbody for n in ast.walk(st)}

    releases = [n for n in ast.walk(fn) if _is_release(n)]
    ends = [n for n in ast.walk(fn) if _is_end_record(n)]
    assert releases and ends, "проверяли не тот метод"
    assert all(id(r) in in_final for r in releases), (
        "логгер освобождается до записи subagent_end"
    )
    assert not any(id(e) in in_final for e in ends)
