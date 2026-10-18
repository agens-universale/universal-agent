"""Подтверждение `y/n` на Windows: опрос консоли, а не `input()` в потоке.

На Windows `select` к консоли неприменим, и подтверждение читало ответ
`input()` в рабочем потоке гарда. После Ctrl+C поток бросали, но он оставался в
чтении и забирал нажатия, адресованные уже следующему вопросу. Решение по ним
не записывалось — это отсекает метка «брошен», — но само нажатие пропадало.

Консоль здесь поддельная: очередь нажатий, какой её видит `msvcrt`. Её
`readline` делает то же, что `input()` в потоке, — ждёт Enter и забирает
нажатия, — поэтому прежний путь краснеет по-настоящему, а не на заглушке.
"""

import asyncio
import collections
import concurrent.futures
import io
import sys
import threading
import time

import pytest
from gc_core.security import path_policy
from gc_core.security.confirm_port import (
    CONFIRM_ALLOW_ONCE,
    CONFIRM_ALLOW_REMEMBER,
    CONFIRM_DENY_ONCE,
)

_THREAD = "test-windows-confirm"
_ALLOWED = (CONFIRM_ALLOW_ONCE, CONFIRM_ALLOW_REMEMBER)
_QUESTION = dict(
    title="Запись", fields=[("path", "C:/SECRET")], question="Разрешить?"
)


class _Console:
    """Консоль Windows: нажатия в очереди, `kbhit`/`getwch`/`putwch` — как у `msvcrt`."""

    def __init__(self) -> None:
        self._keys: collections.deque[str] = collections.deque()
        self._cv = threading.Condition()
        self._closed = False
        self.echo: list[str] = []
        #: Вопрос уже читают: опрос дошёл до пустой очереди или начат `readline`.
        #: Человек отвечает после того, как увидел вопрос, — тесты ждут этого,
        #: а не угадывают паузой: первый импорт тулов в потоке длится дольше.
        self.reading = threading.Event()

    # --- человек за клавиатурой
    def type(self, text: str) -> None:
        with self._cv:
            self._keys.extend(text)
            self._cv.notify_all()

    def remaining(self) -> str:
        with self._cv:
            return "".join(self._keys)

    def close(self) -> None:
        """Выпустить читателя, если он всё-таки ждёт: последний Enter.

        Иначе сломанный опрос висел бы вечно, а поток пула держал бы выход
        интерпретатора.
        """
        with self._cv:
            self._closed = True
            self._keys.append("\r")
            self._cv.notify_all()

    # --- msvcrt
    def kbhit(self) -> bool:
        with self._cv:
            if not self._keys:
                self.reading.set()
            return bool(self._keys)

    def getwch(self) -> str:
        with self._cv:
            return self._keys.popleft()

    def putwch(self, ch: str) -> None:
        self.echo.append(ch)

    # --- sys.stdin: консоль без дескриптора для `select`, чтение — как `input()`
    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        raise io.UnsupportedOperation("fileno")

    def readline(self) -> str:
        self.reading.set()
        with self._cv:
            while "\r" not in self._keys and not self._closed:
                self._cv.wait()
            if "\r" not in self._keys:
                return ""
            line = []
            while (key := self._keys.popleft()) != "\r":
                line.append(key)
            return "".join(line) + "\n"


@pytest.fixture
def console(monkeypatch):
    fake = _Console()
    monkeypatch.setattr(sys, "stdin", fake)
    monkeypatch.setattr(
        path_policy, "_polling_console", lambda: fake, raising=False
    )
    yield fake
    fake.close()  # выпустить читателя, если он всё-таки ждёт
    for thread in [
        t for t in threading.enumerate() if t.name.startswith(_THREAD)
    ]:
        thread.join(5)


@pytest.fixture
def ask():
    """Подтверждение в своём потоке, как его и ждёт гард."""
    pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix=_THREAD)
    yield lambda: pool.submit(
        path_policy.prompt_user_confirmation_choice, **_QUESTION
    )
    pool.shutdown(wait=False, cancel_futures=True)


def test_keys_typed_before_the_question_do_not_answer_it(console, ask):
    """Нажатое, пока агент работал, на подтверждение не отвечает."""
    console.type("y\r")

    answer = ask()
    console.reading.wait(5)  # не дождались — значит, ответ взят сразу
    time.sleep(0.3)
    assert not answer.done(), (
        f"подтверждение ответило само: {answer.result()!r}"
    )
    console.type("n\r")
    assert answer.result(timeout=5) not in _ALLOWED


def test_an_abandoned_question_leaves_the_keys_to_the_next_reader(console):
    """Брошенное по Ctrl+C подтверждение не забирает нажатия следующего вопроса."""
    # Внутри теста: модули `tests/security/` импортируются под заглушкой
    # `gc_core.tools` (см. conftest), и взятый тогда `run_abandonable_in` нёс бы
    # чужую для гарда метку «брошен».
    from gc_core.tools.connect_mcp.abandonable import run_abandonable_in

    async def _scenario() -> None:
        waiting = asyncio.ensure_future(
            run_abandonable_in(
                threading.BoundedSemaphore(1),
                path_policy.prompt_user_confirmation_choice,
                _thread_name=_THREAD,
                **_QUESTION,
            )
        )
        while not console.reading.is_set():  # вопрос показан и ждёт
            await asyncio.sleep(0.01)
        waiting.cancel()  # Ctrl+C: вопрос брошен
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await asyncio.sleep(0.3)  # брошенному — время заметить

    asyncio.run(_scenario())
    console.type("ответ\r")  # это уже для следующего вопроса
    time.sleep(0.4)

    assert console.remaining() == "ответ\r", (
        "нажатия забрал брошенный вопрос y/n"
    )


def test_the_answer_is_edited_like_a_line(console, ask):
    """Стрелки не вставляют мусор, Backspace стирает, набранное видно на экране."""
    answer = ask()
    assert console.reading.wait(5), "вопрос так и не начали читать"
    # стрелка вверх, F1 (у обеих два кода), «x» и Backspace, затем «y» и Enter
    console.type("\xe0H" + "\x00;" + "x\x08" + "y\r")

    assert answer.result(timeout=5) == CONFIRM_ALLOW_REMEMBER
    assert "y" in "".join(console.echo), "ответ не показан на экране"


def test_ctrl_z_ends_the_input(console, ask):
    """Ctrl+Z — конец ввода на Windows, как Ctrl+D на POSIX: запрет, а не
    ожидание. Разовый: конец ввода — не выбор запрета на всю сессию."""
    answer = ask()
    assert console.reading.wait(5), "вопрос так и не начали читать"
    console.type("\x1a")

    assert answer.result(timeout=5) == CONFIRM_DENY_ONCE
