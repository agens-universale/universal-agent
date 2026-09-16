"""Порт «спросить человека» у gc-cli: свой поток для stdin, Ctrl+C — отказ.

Без порта ядро читало stdin само, через `asyncio.to_thread`, и это ломалось
дважды: поток брался из пула `concurrent.futures`, который `asyncio.run` ждёт на
выходе (прерванный прогон висел, пока в stdin не придёт строка), а SIGINT под
`asyncio.run` отменяет главную задачу вместо `KeyboardInterrupt` внутри тула —
ветка «прерывание» в `turn_ask_human` была мёртвой, и Ctrl+C ронял весь прогон.
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
    """stdin, который молчит: человек ещё не ответил."""

    def __init__(self, hold: float = 5.0):
        self._hold = hold

    def __iter__(self):
        deadline = time.monotonic() + self._hold
        while time.monotonic() < deadline:
            time.sleep(0.01)
        return iter(())


def _reader_threads() -> list:
    return [t for t in threading.enumerate() if t.name == READER_THREAD]


@pytest.mark.asyncio
async def test_port_returns_what_the_human_typed(monkeypatch):
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO("синий\n\n"))
    port = make_cli_ask_human_port()

    assert await asyncio.wait_for(port("любимый цвет?"), 5) == "синий"


@pytest.mark.asyncio
async def test_reader_runs_in_a_daemon_thread(monkeypatch):
    """Поток-читатель не должен удерживать процесс на выходе."""
    monkeypatch.setattr(sys, "stdin", _BlockingStdin())
    port = make_cli_ask_human_port()

    task = asyncio.create_task(port("вопрос?"))
    for _ in range(100):
        if _reader_threads():
            break
        await asyncio.sleep(0.01)

    threads = _reader_threads()
    assert threads, "поток чтения не поднялся"
    assert all(t.daemon for t in threads), (
        "поток чтения не daemon — интерпретатор будет ждать его на выходе"
    )

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_ctrl_c_during_the_question_interrupts_only_the_question(monkeypatch):
    """SIGINT обязан стать `KeyboardInterrupt` ВНУТРИ порта.

    Тогда его ловит `acall` тула и отдаёт агенту штатное «отвечать не стали».
    Раньше сигнал отменял главную задачу, и падал весь прогон.
    """
    if not hasattr(signal, "SIGINT"):
        pytest.skip("платформа без SIGINT")
    monkeypatch.setattr(sys, "stdin", _BlockingStdin())
    port = make_cli_ask_human_port()

    async def _ctrl_c() -> None:
        await asyncio.sleep(0.2)
        os.kill(os.getpid(), signal.SIGINT)

    fire = asyncio.create_task(_ctrl_c())
    with pytest.raises(KeyboardInterrupt):
        await asyncio.wait_for(port("вопрос?"), 5)
    await fire


@pytest.mark.asyncio
async def test_signal_handler_is_restored(monkeypatch):
    """Перехват живёт только на время вопроса: дальше Ctrl+C снова общий."""
    import io

    before = signal.getsignal(signal.SIGINT)
    monkeypatch.setattr(sys, "stdin", io.StringIO("ответ\n\n"))

    await asyncio.wait_for(make_cli_ask_human_port()("вопрос?"), 5)

    assert signal.getsignal(signal.SIGINT) is before


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
    assert installs, "gc-cli не ставит ctx.ask_human — Ctrl+C снова обрушит прогон"
