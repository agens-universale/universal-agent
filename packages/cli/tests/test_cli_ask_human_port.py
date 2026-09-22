"""Порт «спросить человека» у gc-cli: stdin читают только во время вопроса.

Первая редакция порта заводила демон-поток на каждый вопрос. Она чинила выход и
Ctrl+C, но принесла свой дефект: отменить поток, сидящий в `readline`, нечем, и
после Ctrl+C он забирал первую строку у следующего читателя — у следующего
вопроса или у подтверждения `y/n` гарда.

Поэтому главная проверка здесь — не «поток демонский», а «читателей не
копится»: на POSIX их нет вовсе, и набранное после прерывания достаётся
следующему вопросу, а не осиротевшему читателю.
"""
import ast
import asyncio
import os
import pathlib
import signal
import sys
import threading
import time

import pytest

from gc_cli.ask_human_port import make_cli_ask_human_port

READER_THREAD = "gc-cli-ask-human"


class _BlockingStdin:
    """stdin без `fileno()`: молчит и уводит порт на запасной путь."""

    def __init__(self, hold: float = 5.0):
        self._hold = hold

    def __iter__(self):
        deadline = time.monotonic() + self._hold
        while time.monotonic() < deadline:
            time.sleep(0.01)
        return iter(())


class _PipeStdin:
    """Настоящий stdin: файловый объект на трубе, с `fileno()`."""

    def __init__(self):
        r, w = os.pipe()
        self._r, self.write_fd = r, w

    def fileno(self) -> int:
        return self._r

    def send(self, text: str) -> None:
        os.write(self.write_fd, text.encode("utf-8"))

    def close_writer(self) -> None:
        os.close(self.write_fd)


class _PtyStdin:
    """stdin — ведомая сторона pty: терминал, как у человека за клавиатурой."""

    def __init__(self):
        import pty

        self.master, self._slave = pty.openpty()

    def fileno(self) -> int:
        return self._slave

    def type(self, text: str) -> None:
        os.write(self.master, text.encode("utf-8"))

    def close(self) -> None:
        os.close(self.master)
        os.close(self._slave)


def _reader_threads() -> list:
    return [t for t in threading.enumerate() if t.name == READER_THREAD]


def _fresh_reader(monkeypatch) -> None:
    """Буфер общий на процесс — между тестами его надо сбрасывать, иначе
    недочитанный хвост одного теста утечёт в следующий."""
    import gc_core.utils.stdin_lines as reader

    reader._PENDING.clear()
    monkeypatch.setattr(reader, "_PARTIAL", "")
    monkeypatch.setattr(reader, "_EOF", False)


@pytest.fixture
def pipe_stdin(monkeypatch):
    _fresh_reader(monkeypatch)
    fake = _PipeStdin()
    monkeypatch.setattr(sys, "stdin", fake)
    yield fake


@pytest.fixture
def pty_stdin(monkeypatch):
    pytest.importorskip("pty")
    _fresh_reader(monkeypatch)
    fake = _PtyStdin()
    monkeypatch.setattr(sys, "stdin", fake)
    yield fake
    fake.close()


@pytest.mark.asyncio
async def test_port_returns_what_the_human_typed(pipe_stdin):
    port = make_cli_ask_human_port()
    pipe_stdin.send("синий\n\n")

    assert await asyncio.wait_for(port("любимый цвет?"), 5) == "синий"


@pytest.mark.asyncio
async def test_no_reader_thread_on_posix(pipe_stdin):
    """На POSIX потока-читателя нет вовсе — значит и осиротеть нечему."""
    port = make_cli_ask_human_port()
    task = asyncio.create_task(port("вопрос?"))
    await asyncio.sleep(0.2)

    assert _reader_threads() == [], "поток-читатель вернулся — вместе с ним вернётся R2.6"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_interrupted_question_leaves_the_line_to_the_next_reader(pipe_stdin):
    """Главная регрессия R2.6: набранное не уходит осиротевшему читателю.

    Человек прерывает первый вопрос и отвечает на второй. Ответ обязан достаться
    ВТОРОМУ вопросу. В прежней редакции первую строку забирал поток, оставшийся
    от первого вопроса, и агент получал пустоту.
    """
    if not hasattr(signal, "SIGINT"):
        pytest.skip("платформа без SIGINT")
    port = make_cli_ask_human_port()

    async def _ctrl_c() -> None:
        await asyncio.sleep(0.2)
        os.kill(os.getpid(), signal.SIGINT)

    # Страховка вместо обработчика asyncio: если перехват сломается, сигнал
    # запишется здесь, и тест покраснеет своим ассертом, а не оборвёт весь
    # прогон pytest через `KeyboardInterrupt` из обработчика раннера.
    caught: list = []
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: caught.append(True))
    try:
        fire = asyncio.create_task(_ctrl_c())
        with pytest.raises(KeyboardInterrupt):
            await asyncio.wait_for(port("первый вопрос?"), 5)
        await fire
    finally:
        signal.signal(signal.SIGINT, previous)
    assert caught == [], "Ctrl+C ушёл мимо перехвата вопроса"

    pipe_stdin.send("ответ-на-второй\n\n")
    assert await asyncio.wait_for(port("второй вопрос?"), 5) == "ответ-на-второй"


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["ctrl_c", "cancel"])
async def test_a_draft_for_a_withdrawn_question_does_not_leak_into_the_next(
    pipe_stdin, how,
):
    """Недописанный ответ к снятому вопросу не приклеивается к следующему.

    Человек начал отвечать и передумал — нажал Ctrl+C, или прогон отменили.
    Набранное уже прочитано в общий буфер, и без сброса оно стало бы началом
    ответа на СЛЕДУЮЩИЙ вопрос.
    """
    if how == "ctrl_c" and not hasattr(signal, "SIGINT"):
        pytest.skip("платформа без SIGINT")
    port = make_cli_ask_human_port()
    pipe_stdin.send("черновик\n")

    # Страховка ставится ДО вопроса: вопрос запоминает её как прежний
    # обработчик, а поставленная после — перебила бы сам перехват.
    caught: list = []
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: caught.append(True))
    try:
        if how == "ctrl_c":
            async def _ctrl_c() -> None:
                await asyncio.sleep(0.2)           # черновик уже в буфере
                os.kill(os.getpid(), signal.SIGINT)

            # Вопрос ждём прямо в тесте, а не отдельной задачей: из задачи
            # `KeyboardInterrupt` asyncio пробрасывает сквозь loop, и он обрывает
            # весь прогон pytest, а не попадает в `pytest.raises`.
            fire = asyncio.ensure_future(_ctrl_c())
            with pytest.raises(KeyboardInterrupt):
                await asyncio.wait_for(port("первый вопрос?"), 5)
            await fire
        else:
            first = asyncio.ensure_future(port("первый вопрос?"))
            await asyncio.sleep(0.2)               # черновик уже в буфере
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
    finally:
        signal.signal(signal.SIGINT, previous)
    assert caught == [], "Ctrl+C ушёл мимо перехвата вопроса"

    pipe_stdin.send("ответ\n\n")
    assert await asyncio.wait_for(port("второй вопрос?"), 5) == "ответ"


@pytest.mark.asyncio
async def test_a_batch_of_answers_is_split_between_questions(pipe_stdin):
    """Пачка строк из трубы делится между вопросами, а не достаётся одному."""
    port = make_cli_ask_human_port()
    pipe_stdin.send("первый\n\nвторой\n\n")

    assert await asyncio.wait_for(port("раз?"), 5) == "первый"
    assert await asyncio.wait_for(port("два?"), 5) == "второй"


@pytest.mark.asyncio
async def test_closed_stdin_ends_the_answer(pipe_stdin):
    """EOF — это пустой ответ, а не вечное ожидание."""
    port = make_cli_ask_human_port()
    pipe_stdin.close_writer()

    assert await asyncio.wait_for(port("вопрос?"), 5) == ""


@pytest.mark.asyncio
async def test_a_last_line_without_newline_is_still_the_answer(pipe_stdin):
    """`printf 'abc'` — ответ «abc»: хвост без перевода строки не теряется."""
    port = make_cli_ask_human_port()
    pipe_stdin.send("abc")
    pipe_stdin.close_writer()

    assert await asyncio.wait_for(port("вопрос?"), 5) == "abc"


@pytest.mark.asyncio
async def test_ctrl_d_ends_only_the_current_answer(pty_stdin):
    """Ctrl+D закончил один ответ — следующий вопрос снова ждёт человека.

    Терминальный конец ввода разовый: после Ctrl+D печатать можно дальше. А Ctrl+D
    — документированный способ закончить ответ. Липкий признак превращал весь
    дальнейший диалог в мгновенные пустые ответы. Проверяется на pty: у трубы
    конец ввода навсегда, и разовость на ней не увидеть.
    """
    port = make_cli_ask_human_port()
    pty_stdin.type("l1\n\x04")                 # строка и Ctrl+D в начале следующей
    assert await asyncio.wait_for(port("первый?"), 5) == "l1"

    second = asyncio.ensure_future(port("второй?"))
    await asyncio.sleep(0.3)
    assert not second.done(), f"второй вопрос ответил сам: {second.result()!r}"
    pty_stdin.type("ответ-2\n\n")
    assert await asyncio.wait_for(second, 5) == "ответ-2"


@pytest.mark.asyncio
async def test_the_fallback_path_leaves_ctrl_c_alone(monkeypatch):
    """Там, где читает поток, перехвата Ctrl+C нет.

    Поток-читатель прервать нечем: перехвати мы Ctrl+C, он остался бы в `read()`
    и забрал строку у следующего вопроса. Без перехвата Ctrl+C снимает прогон —
    так было до перехвата, и это честнее, чем кража ответа.
    """
    monkeypatch.setattr(sys, "stdin", _BlockingStdin())
    before = signal.getsignal(signal.SIGINT)

    task = asyncio.create_task(make_cli_ask_human_port()("вопрос?"))
    await asyncio.sleep(0.2)
    during = signal.getsignal(signal.SIGINT)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert during is before, "на запасном пути поставлен перехват Ctrl+C"


@pytest.mark.asyncio
async def test_signal_handler_is_restored(pipe_stdin):
    """Перехват живёт только на время вопроса: дальше Ctrl+C снова общий."""
    before = signal.getsignal(signal.SIGINT)
    pipe_stdin.send("ответ\n\n")

    await asyncio.wait_for(make_cli_ask_human_port()("вопрос?"), 5)

    assert signal.getsignal(signal.SIGINT) is before


@pytest.mark.asyncio
async def test_stdin_without_fileno_falls_back_to_a_daemon_thread(monkeypatch):
    """Windows и подменённый stdin: запасной путь остаётся, и он демонский."""
    monkeypatch.setattr(sys, "stdin", _BlockingStdin())
    port = make_cli_ask_human_port()

    task = asyncio.create_task(port("вопрос?"))
    for _ in range(100):
        if _reader_threads():
            break
        await asyncio.sleep(0.01)

    threads = _reader_threads()
    assert threads, "запасной путь не поднял читателя"
    assert all(t.daemon for t in threads), "читатель не daemon — задержит выход"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_cli_installs_the_port():
    """Порт должен быть проводкой CLI, иначе ядро снова уйдёт в `to_thread`.

    Модуль читаем по пути, а не импортируем: `cli.py` тянет `gc_cli.arg_parser`,
    а тот на уровне модуля зовёт `parse_known_args()` — на аргументах pytest это
    `SystemExit: 2`.
    """
    import importlib.util

    origin = importlib.util.find_spec("gc_cli.cli").origin
    tree = ast.parse(pathlib.Path(origin).read_text(encoding="utf-8"))
    installs = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(getattr(t, "attr", None) == "ask_human" for t in n.targets)
    ]
    assert len(installs) >= 2, (
        "ctx.ask_human ставится не на всех входах CLI — в `--init` вопрос агента "
        "снова пойдёт через input() в потоке пула"
    )
