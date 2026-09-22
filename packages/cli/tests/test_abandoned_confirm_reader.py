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
import signal
import sys
import threading

import pytest

pty = pytest.importorskip("pty")

from gc_cli.ask_human_port import make_cli_ask_human_port
from gc_core.security.confirm_port import CONFIRM_ALLOW_ONCE, CONFIRM_ALLOW_REMEMBER
from gc_core.security.path_policy import prompt_user_confirmation_choice

_THREAD = "test-abandoned-yn"
_ALLOWED = (CONFIRM_ALLOW_ONCE, CONFIRM_ALLOW_REMEMBER)


def _ask_yes_no():
    """Подтверждение гарда — в своём потоке, как его и ждёт гард."""
    # В момент вызова, а не на уровне модуля. В полном прогоне тест-кит
    # (`ensure_real_tools_package`) выселяет пакет `gc_core.tools` и грузит его
    # заново, а подтверждение берёт метку «брошен» своим ленивым импортом — уже
    # из нового экземпляра. Взятый при сборке `run_abandonable_in` ставил бы
    # метку в старом, брошенный поток её не видел и забирал чужую строку: тест
    # краснел в полном прогоне, хотя код верен.
    from gc_core.tools.connect_mcp.abandonable import run_abandonable_in

    return asyncio.ensure_future(
        run_abandonable_in(
            threading.BoundedSemaphore(1),
            prompt_user_confirmation_choice,
            title="Запись",
            fields=[("path", "/etc/SECRET")],
            question="Разрешить?",
            _thread_name=_THREAD,
        )
    )


@pytest.fixture
def safety_net():
    """Обработчик «на всякий случай» вместо обработчика asyncio.

    Без него регрессия перехвата не краснит тест, а снимает задачу — и
    `KeyboardInterrupt` обрывает ВЕСЬ прогон pytest. Со страховкой сигнал,
    прошедший мимо перехвата, просто записывается, и тест падает на своём ассерте.
    """
    caught: list = []
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: caught.append(True))
    try:
        yield caught
    finally:
        signal.signal(signal.SIGINT, previous)


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
    waiting = _ask_yes_no()
    await asyncio.sleep(0.3)  # вопрос y/n показан и ждёт
    waiting.cancel()  # Ctrl+C: вопрос брошен
    with pytest.raises(asyncio.CancelledError):
        await waiting
    await asyncio.sleep(0.3)  # брошенному — время заметить

    # Человек отвечает уже на СЛЕДУЮЩИЙ вопрос агента — печатая, пока тот ещё
    # не начал читать. Пауза и есть жизненный порядок: без неё следующий
    # читатель успевал раньше брошенного, и тест проходил даже без метки
    # «брошен».
    os.write(terminal, "ответ-на-второй\n\n".encode())
    await asyncio.sleep(0.4)
    answer = await asyncio.wait_for(make_cli_ask_human_port()("второй вопрос?"), 5)

    assert answer == "ответ-на-второй", f"строку забрал брошенный вопрос y/n, следующему досталось {answer!r}"


async def test_a_line_for_a_cancelled_question_does_not_answer_the_confirmation(
    terminal,
    safety_net,
):
    """Строка к отменённому вопросу не отвечает за человека на подтверждении.

    Человек начал отвечать на вопрос агента, передумал и нажал Ctrl+C. Набранное
    уже прочитано в общий буфер — и следующим читателем оказывается подтверждение
    гарда: оно отвечало само и записывало разрешение на путь, о котором человека
    не спрашивали.
    """

    async def _human() -> None:
        await asyncio.sleep(0.2)
        os.write(terminal, b"y\n")  # начал отвечать…
        await asyncio.sleep(0.3)  # …порт уже прочитал строку в буфер
        os.kill(os.getpid(), signal.SIGINT)  # …и передумал

    fire = asyncio.ensure_future(_human())
    with pytest.raises(KeyboardInterrupt):
        await asyncio.wait_for(make_cli_ask_human_port()("вопрос агента?"), 5)
    await fire
    assert safety_net == [], "Ctrl+C ушёл мимо перехвата вопроса"

    confirm = _ask_yes_no()
    await asyncio.sleep(0.5)
    assert not confirm.done(), f"подтверждение ответило само: {confirm.result()!r}"
    os.write(terminal, b"n\n")  # отвечает человек — уже сам
    assert await asyncio.wait_for(confirm, 5) not in _ALLOWED


async def test_input_typed_before_the_confirmation_does_not_answer_it(terminal):
    """Набранное заранее — пока вопроса y/n ещё не было — на него не отвечает.

    Подтверждение безопасности принимает только ввод, сделанный ПОСЛЕ того, как
    вопрос показан: иначе случайное `y`, напечатанное, пока агент работал,
    выдавало бы разрешение без вопроса.
    """
    os.write(terminal, b"y\n")
    await asyncio.sleep(0.1)

    confirm = _ask_yes_no()
    await asyncio.sleep(0.5)
    assert not confirm.done(), f"подтверждение ответило само: {confirm.result()!r}"
    os.write(terminal, b"n\n")
    assert await asyncio.wait_for(confirm, 5) not in _ALLOWED
