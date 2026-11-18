"""Выход процесса, когда терминал умер.

Textual смерть терминала не переживает. Поток вывода падает на первой же записи
(EIO), его очередь на 30 записей забивается за пару перерисовок, и главный поток
навсегда встаёт в `Queue.put`; поток ввода при этом крутит пустое чтение на 100%
CPU. Штатный выход Textual отсюда невозможен — он сам пишет в ту же очередь.
Поэтому выход идёт мимо Textual: своя уборка и хуки atexit — параллельно, с общим
бюджетом времени → `os._exit`. Хуки atexit `os._exit` пропускает, а на них
держатся обещания на выходе процесса — закрытие долгоживущих stdio-MCP-серверов,
сброс логов.

Оба пути сюда — из отдельного потока-сторожа, главный поток не нужен:
- сигнал (SIGHUP; у gc-ua ещё SIGTERM — спека §5.12): номер сигнала пишет в
  wakeup fd C-обработчик интерпретатора — из любого потока, даже когда главный
  стоит в ожидании лока и Python-обработчик там не выполнится;
- сигнал не дошёл (лидер сессии его игнорирует, обёртка не пробрасывает): раз в
  POLL_S проверяем, жив ли терминал, в который рисует Textual (stderr).
"""

from __future__ import annotations

import atexit
import contextlib
import errno
import os
import select
import signal
import threading
import time
from typing import Callable

# Выход по мёртвому терминалу — EXIT_CODE, по сигналу сторожа — 128 + номер.
EXIT_CODE = 128 + 1  # как у процесса, убитого SIGHUP
POLL_S = 1.0  # период проверки терминала
# Сигнал сторожа по умолчанию — обрыв терминала. На Windows SIGHUP нет: сторож
# там не ставится (`install`), и сигналов у него нет.
HANGUP_SIGNALS: tuple[int, ...] = (
    (signal.SIGHUP,) if hasattr(signal, "SIGHUP") else ()
)
# Повтор этих сигналов — немедленный выход (спека §5.12): после первого сторож
# убирается, а второй действует по умолчанию. Повторный SIGHUP уборку не
# обрывает: обрыв терминала может прийти дважды — от ядра и от оболочки.
_REPEAT_EXITS_AT_ONCE = (signal.SIGTERM,)
# Уборка идёт шагами, и каждый ограничен graceful-таймаутом супервизора (5 с)
# независимо от числа демонов: все MCP-демоны разом, затем голос. Параллельно
# хуки atexit закрывают stdio-MCP-серверы (браузер) — до ~15 с на сессию. Бюджет
# покрывает худший случай с запасом, так что SIGKILL глухим к SIGTERM уходит
# раньше os._exit; в обычном случае выход не ждёт — сторож ждёт ровно до конца
# уборки. Не уложилась уборка — перед выходом добивает `force_kill`.
CLEANUP_BUDGET_S = 20.0


def _tty_alive(fd: int) -> bool:
    """Терминал на `fd` жив. Проверка — запись нуля байт: на живом терминале она
    ничего не выводит, на повешенном падает с EIO (или ENXIO) и на Linux, и на
    macOS. Прочие ошибки — не признак смерти: неблокирующий tty, пока в него пишет
    поток вывода Textual, может ответить EAGAIN, и выход из живого TUI был бы ложным.
    Ни tcgetattr, ни isatty не годятся: на macOS повешенный tty, пока жив лидер
    сессии, отвечает на них как живой."""
    try:
        os.write(fd, b"")
    except OSError as error:
        return error.errno not in (errno.EIO, errno.ENXIO)
    return True


def _silence_output() -> None:
    """fd 1 и 2 → /dev/null. Любой вывод в мёртвый терминал — OSError, и первый же
    print уборки (гашение MCP печатает «[stop] …») оборвал бы её."""
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 1)
    os.dup2(null, 2)


def _quiet(signum: int, frame: object) -> None:
    """Сигнал разбирает сторож по wakeup fd; здесь делать нечего."""


def _default_from_now_on(signum: int, frame: object) -> None:
    """Номер сигнала уже в wakeup fd — сторож уберётся и выйдет; повторный
    сигнал действует по умолчанию и завершает процесс сразу."""
    del frame
    signal.signal(signum, signal.SIG_DFL)


def _handler_for(signum: int) -> Callable[[int, object], None]:
    return _default_from_now_on if signum in _REPEAT_EXITS_AT_ONCE else _quiet


class TerminalGuard:
    """Сторож терминала: при его смерти — уборка и выход процесса."""

    def __init__(
        self,
        cleanup: Callable[[], None],
        *,
        force_kill: Callable[[], None] | None = None,
        signals: tuple[int, ...] = HANGUP_SIGNALS,
    ) -> None:
        """`force_kill` — последний рубеж: если уборка не завершилась (на аварийном
        выходе — зависла, упала или не стартовала; на штатном — упала), он добивает
        то, что она гасила. Однократный. Обязан не блокировать: сигнал и
        возврат. `signals` — сигналы, по которым сторож выходит с уборкой
        (код 128 + номер сигнала)."""
        self._cleanup = cleanup
        self._force_kill = force_kill
        self._signals = tuple(signals)
        self._cleaned = False
        self._cleanup_done = False
        self._closed = False
        self._watcher: threading.Thread | None = None
        self._wake_r = self._wake_w = -1
        self._previous_handlers: dict[int, object] = {}
        self._previous_wakeup = -1
        self.trigger: str | None = (
            None  # "signal" | "tty" — какой путь сработал
        )

    def install(self) -> None:
        """Поставить сторожа — из главного потока. На Windows нет ни SIGHUP, ни
        tty-семантики: ничего не делаем."""
        if not hasattr(signal, "SIGHUP"):
            return
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_w, False)
        # Сначала wakeup fd, потом обработчики: сигнал, попавший в наш
        # обработчик, всегда записан в канал, и сторож прочтёт его, когда
        # стартует. В обратном порядке сигнал между ними проглотил бы
        # обработчик, а в канал он бы не попал.
        self._previous_wakeup = signal.set_wakeup_fd(
            self._wake_w, warn_on_full_buffer=False
        )
        # Python-обработчик нужен затем, чтобы стоял C-обработчик, пишущий
        # номер сигнала в wakeup fd (у SIGTERM он ещё отдаёт повтор действию
        # по умолчанию). Локов в нём нет — дедлока не будет.
        # Унаследованный SIG_IGN (nohup) перекрываем осознанно: TUI без терминала
        # бесполезен.
        self._previous_handlers = {
            signum: signal.signal(signum, _handler_for(signum))
            for signum in self._signals
        }
        # Опрос — терминала вывода (stderr: туда рисует Textual, и зависание
        # начинается с записи в него), и только если он был терминалом на старте:
        # пайпы, run_test и headless сторож не трогает.
        self._watcher = threading.Thread(
            target=self._watch,
            args=(os.isatty(2),),
            name="gc-terminal-guard",
            daemon=True,
        )
        self._watcher.start()

    def _watch(self, watch_tty: bool) -> None:
        while True:
            ready = select.select([self._wake_r], [], [], POLL_S)[0]
            data = os.read(self._wake_r, 512) if ready else b""
            if self._closed:
                return
            # В wakeup fd пишутся все сигналы с Python-обработчиком (SIGWINCH,
            # SIGTSTP, SIGCONT у Textual) — нас касаются только свои.
            caught = next((s for s in self._signals if s in data), None)
            if caught is not None:
                self.trigger = "signal"
                code = 128 + caught
            elif watch_tty and not _tty_alive(2):
                self.trigger = "tty"
                code = EXIT_CODE
            else:
                continue
            self._emergency_exit(code)
            return

    def _emergency_exit(self, code: int = EXIT_CODE) -> None:
        """Своя уборка и хуки atexit — в двух потоках разом, с общим дедлайном:
        зависшая уборка не съедает время хуков. `_run_exitfuncs` — приватный, но
        тот же вызов, что делает интерпретатор на штатном выходе; публичного нет.
        `os._exit` — в `finally`: исключение аварийного пути (поток не стартовал)
        иначе молча убило бы сторожа, и процесс снова висел бы. Не вышло заглушить
        вывод (нет свободных fd) — убираемся всё равно: печать гашения MCP мёртвый
        терминал терпит сама. Уборка не завершилась — перед выходом последний
        рубеж `force_kill`: живыми демонов не оставляем, что бы в уборке ни
        зависло."""
        try:
            with contextlib.suppress(OSError):
                _silence_output()
            deadline = time.monotonic() + CLEANUP_BUDGET_S
            workers = [
                threading.Thread(target=target, daemon=True)
                for target in (self.run_cleanup, atexit._run_exitfuncs)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(max(0.0, deadline - time.monotonic()))
        finally:
            self._force_kill_if_unfinished()
            os._exit(code)

    def _force_kill_if_unfinished(self) -> None:
        """Последний рубеж — однократно и только если уборка не завершилась."""
        force_kill, self._force_kill = self._force_kill, None
        if force_kill is not None and not self._cleanup_done:
            with contextlib.suppress(Exception):
                force_kill()

    def _finish_cleanup(self) -> None:
        """Штатная уборка; упала — добиваем, как на аварийном пути, а ошибка летит
        дальше."""
        try:
            self.run_cleanup()
        finally:
            self._force_kill_if_unfinished()

    def run_cleanup(self) -> None:
        """Уборка — ровно один раз: повторный `close()` её не повторит. Штатная и
        аварийная уборка не пересекаются: `close()` сначала снимает сторожа, а
        аварийный путь кончается `os._exit`."""
        if self._cleaned:
            return
        self._cleaned = True
        self._cleanup()
        self._cleanup_done = True

    def close(self) -> None:
        """Штатный выход: снять сторожа, убраться, вернуть сигналы как были.
        Обработчики возвращаются ПОСЛЕ уборки — сигнал посреди неё её не
        оборвёт; и даже если уборка упала."""
        if self._watcher is None:
            self._finish_cleanup()
            return
        self._closed = True
        try:
            os.write(self._wake_w, b"\0")
        except BlockingIOError:
            pass  # канал полон — значит, сторожу и так есть что читать, он проснётся
        self._watcher.join()
        self._watcher = None
        try:
            self._finish_cleanup()
        finally:
            for signum, previous in self._previous_handlers.items():
                signal.signal(
                    signum, signal.SIG_DFL if previous is None else previous
                )
            signal.set_wakeup_fd(self._previous_wakeup)
            os.close(self._wake_r)
            os.close(self._wake_w)
