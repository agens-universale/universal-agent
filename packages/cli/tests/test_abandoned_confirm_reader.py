"""Брошенный вопрос y/n не забирает строку у следующего читателя.

Подтверждение гарда читало ответ `input()` в рабочем потоке. После Ctrl+C поток
бросали, но он оставался в `read()` и забирал первую же строку — набранную уже
для СЛЕДУЮЩЕГО вопроса агента. Следующий вопрос своей строки не получал, а
брошенный доигрывал подтверждение по чужому нажатию.

Проверяется на настоящем терминале (pty): только там `input()` и `select`
ведут себя так, как у человека за клавиатурой.
"""
import asyncio
import os
import sys
import threading

import pytest

pty = pytest.importorskip("pty")

from gc_cli.ask_human_port import make_cli_ask_human_port
from gc_core.security.path_policy import prompt_user_confirmation_choice
from gc_core.tools.connect_mcp.abandonable import run_abandonable_in

_THREAD = "test-abandoned-yn"


def _fresh_reader(monkeypatch) -> None:
    """Буфер читателя общий на процесс — сбросить, чтобы тесты не делили хвосты."""
    import gc_core.utils.stdin_lines as reader

    reader._PENDING.clear()
    monkeypatch.setattr(reader, "_PARTIAL", "")
    monkeypatch.setattr(reader, "_EOF", False)


@pytest.fixture
def terminal(monkeypatch):
    """stdin — ведомая сторона pty; тест печатает «за человека» в ведущую."""
    master, slave = pty.openpty()
    stdin = os.fdopen(slave, "r", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", stdin)
    _fresh_reader(monkeypatch)
    yield master
    # Закрытие ведущей стороны будит читателя, если он всё-таки остался в
    # `read()`: он получит EIO и выйдет, а не повиснет до конца процесса.
    os.close(master)
    for thread in [t for t in threading.enumerate() if t.name == _THREAD]:
        thread.join(5)
    stdin.close()


async def test_an_abandoned_yes_no_prompt_leaves_the_next_line_alone(terminal):
    waiting = asyncio.ensure_future(run_abandonable_in(
        threading.BoundedSemaphore(1), prompt_user_confirmation_choice,
        title="Запись", fields=[("path", "/etc/SECRET")], question="Разрешить?",
        _thread_name=_THREAD,
    ))
    await asyncio.sleep(0.3)                    # вопрос y/n показан и ждёт
    waiting.cancel()                            # Ctrl+C: вопрос брошен
    with pytest.raises(asyncio.CancelledError):
        await waiting
    await asyncio.sleep(0.3)                    # брошенному — время заметить

    # Человек отвечает уже на СЛЕДУЮЩИЙ вопрос агента.
    os.write(terminal, "ответ-на-второй\n\n".encode("utf-8"))
    answer = await asyncio.wait_for(make_cli_ask_human_port()("второй вопрос?"), 5)

    assert answer == "ответ-на-второй", (
        f"строку забрал брошенный вопрос y/n, следующему досталось {answer!r}"
    )
