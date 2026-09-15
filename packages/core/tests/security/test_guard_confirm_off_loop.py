"""Подтверждение человека в гардах ждётся в потоке, а не на event loop.

Тело гарда исполняет драйвер `dual_wrap`: блокирующее уходит в поток через
`yield OffLoop(...)`. Защита чтения (режим ask) и защищённые пути звали
`request_confirm` прямо в теле — в асинхронном пути ожидание человека (в TUI до
600 с) шло на event loop агента. Модерация команд делала это правильно, кроме
ветки «судья недоступен» внутри `decide_command_execution`.
"""
import ast
import pathlib

from gc_core.security import command_guard, command_moderation, read_guard


def _direct_confirm_calls(module) -> list:
    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
    return [
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "request_confirm"
    ]


def test_guard_bodies_never_call_confirm_inline():
    for module in (read_guard, command_guard):
        assert _direct_confirm_calls(module) == [], (
            f"{module.__name__}: request_confirm вызван инлайн — "
            "ожидание человека пойдёт на event loop"
        )


def test_moderation_decision_does_not_ask_the_human():
    tree = ast.parse(pathlib.Path(command_moderation.__file__).read_text(encoding="utf-8"))
    decide = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                  and n.name == "decide_command_execution")
    calls = [n for n in ast.walk(decide)
             if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "request_confirm"]
    assert calls == [], "decide_command_execution снова спрашивает человека сама"
