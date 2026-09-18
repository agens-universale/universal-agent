"""Один обработчик Ctrl+C на процесс и стек ожиданий человека.

Перехват ставится на время ожидания: гард ждёт подтверждения, CLI — ответа на
вопрос. Пока каждое ожидание само запоминало обработчик на входе и возвращало на
выходе, два ожидания, вышедшие не в порядке входа, затирали друг другу
обработчик: Ctrl+C то снимал прогон при живом вопросе, то после обоих не делал
вообще ничего.
"""
import signal
import threading

import pytest

from gc_core.utils.sigint_waits import intercept_sigint

pytestmark = pytest.mark.skipif(
    not hasattr(signal, "raise_signal"), reason="платформа без raise_signal"
)


@pytest.fixture
def base():
    """Обработчик «до всех ожиданий» — записывает, сколько раз до него дошло."""
    hits: list = []
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: hits.append(1))
    try:
        yield hits
    finally:
        signal.signal(signal.SIGINT, previous)


def test_ctrl_c_goes_to_the_wait_still_open_after_an_earlier_one_left(base):
    a_hits: list = []
    b_hits: list = []
    first = intercept_sigint(lambda: a_hits.append(1))
    second = intercept_sigint(lambda: b_hits.append(1))
    first.__enter__()
    second.__enter__()
    try:
        first.__exit__(None, None, None)         # A вышел раньше B
        signal.raise_signal(signal.SIGINT)
        assert (a_hits, b_hits, base) == ([], [1], []), (
            f"сигнал ушёл не тому: A={a_hits} B={b_hits} до-всех={base}"
        )
    finally:
        second.__exit__(None, None, None)


def test_with_two_open_waits_ctrl_c_goes_to_the_latest(base):
    """Открыты оба — сигнал у последнего: это вопрос, который человек видит."""
    a_hits: list = []
    b_hits: list = []
    with intercept_sigint(lambda: a_hits.append(1)):
        with intercept_sigint(lambda: b_hits.append(1)):
            signal.raise_signal(signal.SIGINT)

    assert (a_hits, b_hits, base) == ([], [1], []), (
        f"сигнал ушёл не последнему ожиданию: A={a_hits} B={b_hits} до-всех={base}"
    )


def test_the_handler_from_before_the_first_wait_returns_after_the_last(base):
    handler = signal.getsignal(signal.SIGINT)
    with intercept_sigint(lambda: None):
        with intercept_sigint(lambda: None):
            pass

    assert signal.getsignal(signal.SIGINT) is handler, "обработчик не вернулся"
    signal.raise_signal(signal.SIGINT)
    assert base == [1], "после последнего ожидания Ctrl+C до общего обработчика не дошёл"


def test_off_the_main_thread_nothing_is_intercepted(base):
    """Обработчик ставится только из главного потока — вне его ожидание честно
    сообщает, что перехвата нет, и ничего не трогает."""
    handler = signal.getsignal(signal.SIGINT)
    armed: list = []

    def _worker() -> None:
        with intercept_sigint(lambda: None) as on:
            armed.append(on)

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join(5)

    assert armed == [False]
    assert signal.getsignal(signal.SIGINT) is handler
