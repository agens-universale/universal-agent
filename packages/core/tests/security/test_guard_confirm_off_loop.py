"""Подтверждение человека в гардах ждётся в потоке, а не на event loop.

Тело гарда исполняет драйвер `dual_wrap`: блокирующее уходит в поток через
`yield OffLoop(...)`. Защита чтения (режим ask) и защищённые пути звали
`request_confirm` прямо в теле — в асинхронном пути ожидание человека (в TUI до
600 с) шло на event loop агента. Модерация команд делала это правильно, кроме
ветки «судья недоступен» внутри `decide_command_execution`.
"""
import ast
import pathlib

from gc_core.security import (
    command_guard,
    command_moderation,
    path_policy,
    read_guard,
    url_policy,
)


def _guard_body(module) -> ast.FunctionDef:
    """Тело гарда — генератор `_flow`, который исполняет драйвер `dual_wrap`."""
    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_flow")


def _direct_confirm_calls(module) -> list:
    """Вызовы `request_confirm` В ТЕЛЕ гарда — те, что исполнит event loop.

    Смотрим тело, а не модуль целиком: помощник вроде
    `_ask_user_confirmation_for_write` зовёт `request_confirm` законно — он сам
    уезжает в поток через `OffLoop`, и запрет на него был бы ложной тревогой.
    """
    return [
        n.lineno for n in ast.walk(_guard_body(module))
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "request_confirm"
    ]


def test_guard_bodies_never_call_confirm_inline():
    # Список полный НЕ случайно: сначала перевели только защиту чтения и команд,
    # а гард записи, защищённые пути и политика URL остались ждать человека на
    # loop — притом что `ask` у них режим по умолчанию, то есть обычный путь.
    for module in (read_guard, command_guard, path_policy, url_policy):
        assert _direct_confirm_calls(module) == [], (
            f"{module.__name__}: request_confirm вызван инлайн — "
            "ожидание человека пойдёт на event loop"
        )


def test_write_guard_helper_is_not_called_inline_either():
    """`_ask_user_confirmation_for_write` — та же блокировка под другим именем."""
    tree = ast.parse(pathlib.Path(path_policy.__file__).read_text(encoding="utf-8"))
    flow = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_flow")
    inline = [
        n.lineno for n in ast.walk(flow)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", None) == "_ask_user_confirmation_for_write"
    ]
    assert inline == [], f"вызов инлайн на строках {inline}: ожидание уйдёт на loop"


def test_download_file_hands_the_write_guard_to_a_thread():
    """У загрузчика гард свой, синхронный, и зовётся он из корутинного `acall`."""
    from gc_core.tools.exec import download_file

    tree = ast.parse(pathlib.Path(download_file.__file__).read_text(encoding="utf-8"))
    acall = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.AsyncFunctionDef) and n.name == "acall")
    direct = [n.lineno for n in ast.walk(acall)
              if isinstance(n, ast.Call)
              and getattr(n.func, "attr", None) == "_check_write_guard"]
    assert direct == [], (
        f"_check_write_guard вызван прямо на строках {direct} — loop встанет на ожидании"
    )
    off_loop = [n for n in ast.walk(acall)
                if isinstance(n, ast.Call)
                and getattr(n.func, "attr", None) == "to_thread"
                and any(getattr(a, "attr", None) == "_check_write_guard" for a in n.args)]
    assert off_loop, "гард записи больше не уезжает в поток — проверка устарела"


def test_moderation_decision_does_not_ask_the_human():
    tree = ast.parse(pathlib.Path(command_moderation.__file__).read_text(encoding="utf-8"))
    decide = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                  and n.name == "decide_command_execution")
    calls = [n for n in ast.walk(decide)
             if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "request_confirm"]
    assert calls == [], "decide_command_execution снова спрашивает человека сама"
