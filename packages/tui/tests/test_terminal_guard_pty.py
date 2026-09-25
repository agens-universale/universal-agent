"""Сторож терминала на настоящем pty: терминал умер → уборка и выход 129.

Баг проявляется только на живом терминале, поэтому здесь не моки. Если терминал
умер, а SIGHUP до процесса не дошёл, Textual виснет навсегда: главный поток стоит
в `Queue.put`, поток ввода крутит пустое чтение. Если SIGHUP дошёл, а диспозиция
по умолчанию, процесс умирает без уборки. Здесь приложение Textual со сторожем
запускается под `pty.fork`, а «смерть терминала» — это закрытие master-стороны.

Только POSIX: на Windows нет ни pty, ни SIGHUP.
"""
from __future__ import annotations

import os
import select
import signal
import struct
import sys
import threading
import time
from pathlib import Path

import pytest

termios = pytest.importorskip("termios", reason="только POSIX: нужен настоящий pty")
# Только после importorskip: на Windows `import pty` падает на импорте.
import fcntl
import pty

from gc_testkit.paths import PACKAGE_SRC_DIRS
from gc_tui.services.terminal_guard import EXIT_CODE

READY_TIMEOUT_S = 30.0
EXIT_TIMEOUT_S = 10.0
STOP_TIMEOUT_S = 10.0

# Приложение под pty. Файл `ready` — смонтировалось; файл `cleanup` — уборка
# прошла, внутри — путь, которым пришёл выход; файл `atexit` — отработали хуки
# выхода процесса.
APP = '''
import atexit, os, sys
from gc_tui.services import terminal_guard
from gc_tui.services.terminal_guard import TerminalGuard
from textual.app import App
from textual.widgets import Static

work = sys.argv[1]
terminal_guard.POLL_S = 0.2

def cleanup():
    with open(os.path.join(work, "cleanup"), "w") as f:
        f.write(guard.trigger or "close")

# Хук выхода процесса — как у реестра stdio-MCP-сессий: os._exit его пропускает.
atexit.register(lambda: open(os.path.join(work, "atexit"), "w").close())

class Spin(App):
    def compose(self):
        yield Static("0", id="n")
    def on_mount(self):
        self.n = 0
        self.set_interval(0.05, self.tick)
        open(os.path.join(work, "ready"), "w").close()
    def tick(self):
        self.n += 1
        self.query_one("#n", Static).update(f"tick {self.n}")

guard = TerminalGuard(cleanup)
guard.install()
try:
    Spin().run()
finally:
    guard.close()
'''

# Лидер сессии — sh, который игнорирует HUP и не пробрасывает его. Вторая команда
# (`exit $?`) не даёт sh заменить себя приложением через exec и возвращает его код.
SH_SWALLOWS_HUP = ["/bin/sh", "-c", 'trap "" HUP; "$0" "$@"; exit $?']


def _child_env() -> dict[str, str]:
    """Окружение приложения: исходники проверяемого дерева впереди. Иначе ребёнок
    импортирует `gc_tui` оттуда, куда указывает editable-установка окружения, —
    из другого чекаута, а не из того, что проверяем."""
    sources = [str(path) for path in PACKAGE_SRC_DIRS]
    previous = os.environ.get("PYTHONPATH")
    if previous:
        sources.append(previous)
    return dict(os.environ, TERM="xterm-256color", PYTHONPATH=os.pathsep.join(sources))


def _describe(status: int) -> str:
    if os.WIFEXITED(status):
        return f"код выхода {os.WEXITSTATUS(status)}"
    if os.WIFSIGNALED(status):
        return f"убит сигналом {signal.Signals(os.WTERMSIG(status)).name}"
    return f"статус {status:#x}"


class _PtyApp:
    """Процесс под pty и сторона терминала: master, который постоянно вычитывается."""

    def __init__(self, argv: list[str], work: Path) -> None:
        self.work = work
        env = _child_env()   # до fork: ребёнок лишь зовёт execve
        self.pid, self.master = pty.fork()
        if self.pid == 0:
            try:
                fcntl.ioctl(1, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
                os.execve(argv[0], argv, env)
            finally:
                os._exit(127)   # ребёнок никогда не возвращается в pytest
        self._output = bytearray()
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        """Вычитывать master без перерыва: иначе буфер pty переполнится, и
        приложение зависнет на записи — по другой причине, не от проверяемого бага."""
        while not self._stop.is_set():
            try:
                if not select.select([self.master], [], [], 0.05)[0]:
                    continue
                chunk = os.read(self.master, 65536)
            except OSError:
                return
            if not chunk:
                return
            self._output += chunk

    def _tail(self) -> str:
        return bytes(self._output[-2000:]).decode(errors="replace")

    def wait_ready(self) -> None:
        deadline = time.monotonic() + READY_TIMEOUT_S
        while not (self.work / "ready").exists():
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                pytest.fail(
                    f"приложение умерло до готовности ({_describe(status)}); "
                    f"хвост вывода:\n{self._tail()}"
                )
            if time.monotonic() > deadline:
                pytest.fail(f"приложение не поднялось за {READY_TIMEOUT_S:.0f} с")
            time.sleep(0.05)

    def kill_terminal(self) -> None:
        """Смерть терминала — как закрытое окно эмулятора: master закрыт."""
        self._stop.set()
        self._reader.join()
        os.close(self.master)
        self.master = -1

    def wait_exit(self) -> int:
        deadline = time.monotonic() + EXIT_TIMEOUT_S
        while time.monotonic() < deadline:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                return status
            time.sleep(0.05)
        pytest.fail(f"процесс не вышел за {EXIT_TIMEOUT_S:.0f} с после смерти терминала — завис")

    def wait_stopped(self) -> int:
        deadline = time.monotonic() + STOP_TIMEOUT_S
        while time.monotonic() < deadline:
            pid, status = os.waitpid(self.pid, os.WNOHANG | os.WUNTRACED)
            if pid:
                return status
            time.sleep(0.05)
        pytest.fail(f"процесс не остановился по SIGTSTP за {STOP_TIMEOUT_S:.0f} с")

    def dispose(self) -> None:
        # Лидер сессии — он же лидер группы; внук-приложение варианта с sh в той же
        # группе, поэтому SIGKILL — всей группе.
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(self.pid, 0)
        except ChildProcessError:
            pass
        if self.master != -1:
            self.kill_terminal()


@pytest.fixture
def launch(tmp_path):
    """Запустить приложение под pty и дождаться монтирования; после теста —
    добить группу процессов и закрыть терминал."""
    started: list[_PtyApp] = []

    def _launch(*, sh_swallows_hup: bool = False) -> _PtyApp:
        argv = [sys.executable, "-c", APP, str(tmp_path)]
        app = _PtyApp(SH_SWALLOWS_HUP + argv if sh_swallows_hup else argv, tmp_path)
        started.append(app)
        app.wait_ready()
        return app

    yield _launch
    for app in started:
        app.dispose()


def test_sighup_delivered_exits_129_with_cleanup(launch):
    """Лидер сессии — само приложение, ядро шлёт ему SIGHUP. Сторож превращает его
    в выход с уборкой: процесс завершается кодом 129, а не гибнет от сигнала."""
    app = launch()

    app.kill_terminal()
    status = app.wait_exit()

    assert os.WIFEXITED(status), _describe(status)
    assert os.WEXITSTATUS(status) == EXIT_CODE
    assert (app.work / "cleanup").read_text() == "signal"
    assert (app.work / "atexit").exists()


def test_sighup_lost_exits_129_by_tty_poll(launch):
    """Лидер сессии — sh, глотающий HUP: до приложения сигнал не доходит, и без
    сторожа Textual висел бы вечно. Сторож видит мёртвый tty опросом и выходит сам
    (код 129 возвращает sh через `exit $?`)."""
    app = launch(sh_swallows_hup=True)

    app.kill_terminal()
    status = app.wait_exit()

    assert os.WIFEXITED(status), _describe(status)
    assert os.WEXITSTATUS(status) == EXIT_CODE
    assert (app.work / "cleanup").read_text() == "tty"
    assert (app.work / "atexit").exists()


def test_suspend_and_resume_do_not_trigger_exit(launch):
    """Ctrl-Z и возврат: Textual сам обрабатывает SIGTSTP/SIGCONT, их номера тоже
    попадают в wakeup fd сторожа и должны отфильтроваться. Ложного выхода нет, а
    после возврата сторож по-прежнему ловит смерть терминала."""
    app = launch()

    os.kill(app.pid, signal.SIGTSTP)
    status = app.wait_stopped()
    assert os.WIFSTOPPED(status), _describe(status)
    os.kill(app.pid, signal.SIGCONT)
    time.sleep(1.0)   # пять периодов опроса сторожа (POLL_S = 0.2 в APP)

    assert os.waitpid(app.pid, os.WNOHANG) == (0, 0)
    assert not (app.work / "cleanup").exists()

    app.kill_terminal()
    status = app.wait_exit()

    assert os.WIFEXITED(status), _describe(status)
    assert os.WEXITSTATUS(status) == EXIT_CODE
    assert (app.work / "cleanup").read_text() == "signal"
    assert (app.work / "atexit").exists()
