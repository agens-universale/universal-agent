"""Подтверждение человека в гардах ждётся в потоке, а не на event loop.

Тело гарда исполняет драйвер `dual_wrap`: блокирующее уходит в поток через
`yield OffLoop(...)`. Защита чтения (режим ask) и защищённые пути звали
`request_confirm` прямо в теле — в асинхронном пути ожидание человека (в TUI до
600 с) шло на event loop агента. Модерация команд делала это правильно, кроме
ветки «судья недоступен» внутри `decide_command_execution`.
"""

import ast
import pathlib

import pytest
from gc_core.security import (
    command_guard,
    path_policy,
    read_guard,
    url_policy,
)


def _guard_body(module) -> ast.FunctionDef:
    """Тело гарда — генератор `_flow`, который исполняет драйвер `dual_wrap`."""
    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
    return next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_flow"
    )


def _direct_confirm_calls(module) -> list:
    """Вызовы `request_confirm` В ТЕЛЕ гарда — те, что исполнит event loop.

    Смотрим тело, а не модуль целиком: помощник вроде
    `_ask_user_confirmation_for_write` зовёт `request_confirm` законно — он сам
    уезжает в поток через `OffLoop`, и запрет на него был бы ложной тревогой.
    """
    return [
        n.lineno
        for n in ast.walk(_guard_body(module))
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", None) == "request_confirm"
    ]


def test_guard_bodies_never_call_confirm_inline():
    # Список полный НЕ случайно: сначала перевели только защиту чтения и команд,
    # а гард записи, защищённые пути и политика URL остались ждать человека на
    # loop — притом что `ask` у них режим по умолчанию, то есть обычный путь.
    for module in (read_guard, command_guard, path_policy, url_policy):
        assert _direct_confirm_calls(module) == [], (
            f"{module.__name__}: request_confirm вызван инлайн — ожидание человека пойдёт на event loop"
        )


def test_write_guard_helper_is_not_called_inline_either():
    """`_ask_user_confirmation_for_write` — та же блокировка под другим именем."""
    tree = ast.parse(
        pathlib.Path(path_policy.__file__).read_text(encoding="utf-8")
    )
    flow = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_flow"
    )
    inline = [
        n.lineno
        for n in ast.walk(flow)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", None) == "_ask_user_confirmation_for_write"
    ]
    assert inline == [], (
        f"вызов инлайн на строках {inline}: ожидание уйдёт на loop"
    )


async def test_download_file_guard_carries_the_abandoned_mark(
    tmp_path, monkeypatch
):
    """Гард загрузчика едет тем же маршрутом, что остальные пять, — с меткой «брошен».

    Он зовётся не декоратором, а прямо из `acall`, и ехал через
    `asyncio.to_thread`. Метку ставит `run_abandonable_in` внутри снимка
    контекста потока; `to_thread` копирует контекст вызывающего, где она
    пустая. Тогда брошенное по Ctrl+C подтверждение дожидается следующей
    строки и записывает по ней разрешение на запись вне периметра — ровно то,
    что закрывали R4.2 и R5.1. Плюс поток берётся из общего пула, который
    держит выход процесса, а не из слотов гвардов.
    """
    import asyncio
    import threading
    import time

    from gc_core.tools.connect_mcp.abandonable import current_call_abandoned
    from gc_core.tools.exec.download_file import DownloadFile
    from gc_testkit.helpers.tool_ctx import with_local_ctx

    entered, seen = threading.Event(), {}

    def _slow_guard(self, dest):
        entered.set()
        time.sleep(0.3)  # «человек думает» над y/n
        seen["abandoned"] = current_call_abandoned()
        return None

    monkeypatch.setattr(DownloadFile, "_check_write_guard", _slow_guard)
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    tool = with_local_ctx(
        DownloadFile,
        config_dict={
            "launch_root": str(launch_root),
            "download": {"download_dir": "downloads", "max_bytes": 52_428_800},
        },
    )

    task = asyncio.ensure_future(
        tool.acall(url="https://example.com/report.pdf", filename="report.pdf")
    )
    assert await asyncio.to_thread(entered.wait, 5), "гард так и не начался"
    task.cancel()  # Ctrl+C: вызов брошен
    with pytest.raises(asyncio.CancelledError):
        await task

    for _ in range(100):  # гарду дать дописать ответ
        if "abandoned" in seen:
            break
        await asyncio.sleep(0.05)
    assert seen.get("abandoned") is True, (
        "гард загрузчика не видит метку «брошен»: он уехал мимо маршрута гвардов, "
        "и подтверждение запишет решение по чужому нажатию"
    )
