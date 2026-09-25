"""Сторож терминала: при смерти терминала — уборка (однократно, с бюджетом) и выход 129.

Настоящие `dup2` на fd 1/2 и настоящий `os._exit` внутри pytest недопустимы:
первое сломает вывод прогона, второе убьёт его. Поэтому `_silence_output` и
`os._exit` подменены всегда, а гарды закрываются в teardown фикстуры, которая
зависит от подмен, — поток-сторож завершается ДО их отката.
"""

from __future__ import annotations

import atexit
import errno
import os
import signal
import sys
import threading
from types import SimpleNamespace

import pytest

from gc_tui.services import terminal_guard
from gc_tui.services.terminal_guard import TerminalGuard

POLL = 0.05
BUDGET = 0.2
QUIET = 3 * POLL  # «ничего не произошло»: три периода опроса
DEADLINE = 2.0  # потолок ожидания события

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="SIGHUP и tty — только POSIX"
)


class _FakeExit:
    """Подмена `os._exit`: запоминает коды и будит ожидающий тест."""

    def __init__(self) -> None:
        self.codes: list[int] = []
        self.called = threading.Event()

    def __call__(self, code: int) -> None:
        self.codes.append(code)
        self.called.set()


class _Cleanup:
    """Уборка-счётчик."""

    def __init__(self) -> None:
        self.count = 0
        self.done = threading.Event()

    def __call__(self) -> None:
        self.count += 1
        self.done.set()


@pytest.fixture
def patched(monkeypatch):
    exit_ = _FakeExit()
    silenced = threading.Event()
    # Хуки atexit процесса pytest (закрытие логов, финализаторы multiprocessing)
    # настоящими звать нельзя — аварийный путь видит только эту запись. Хуки идут
    # в своём потоке: тест, чей аварийный путь может пережить его конец, ждёт
    # `hooked` — иначе вызов пришёлся бы на уже откаченную, настоящую функцию.
    exit_hooks: list[str] = []
    hooked = threading.Event()

    def run_exitfuncs() -> None:
        exit_hooks.append("atexit")
        hooked.set()

    monkeypatch.setattr(terminal_guard, "_silence_output", silenced.set)
    monkeypatch.setattr(terminal_guard.os, "_exit", exit_)
    monkeypatch.setattr(atexit, "_run_exitfuncs", run_exitfuncs)
    monkeypatch.setattr(terminal_guard, "POLL_S", POLL)
    monkeypatch.setattr(terminal_guard, "CLEANUP_BUDGET_S", BUDGET)
    return SimpleNamespace(
        exit=exit_, silenced=silenced, exit_hooks=exit_hooks, hooked=hooked
    )


@pytest.fixture
def make_guard(patched):
    guards: list[TerminalGuard] = []

    def make(cleanup, force_kill=None) -> TerminalGuard:
        guard = TerminalGuard(cleanup, force_kill=force_kill)
        guards.append(guard)
        return guard

    yield make
    for guard in guards:
        guard.close()


def _guard_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "gc-terminal-guard"]


# --- везде, включая Windows ------------------------------------------------


def test_install_without_sighup_starts_no_watcher(monkeypatch, make_guard):
    monkeypatch.delattr(signal, "SIGHUP", raising=False)
    cleanup = _Cleanup()
    guard = make_guard(cleanup)

    guard.install()
    assert _guard_threads() == []

    guard.close()
    assert cleanup.count == 1


def test_cleanup_runs_once(make_guard):
    cleanup = _Cleanup()
    guard = make_guard(cleanup)

    guard.run_cleanup()
    guard.run_cleanup()
    guard.close()

    assert cleanup.count == 1


def test_emergency_exit_silences_cleans_and_exits_129(patched, make_guard):
    cleanup = _Cleanup()
    guard = make_guard(cleanup)

    guard._emergency_exit()

    assert patched.silenced.is_set()
    assert cleanup.done.wait(DEADLINE)
    assert cleanup.count == 1
    assert patched.hooked.wait(DEADLINE)
    assert patched.exit.codes == [129]


def test_emergency_exit_runs_cleanup_and_exit_hooks(patched, make_guard):
    # os._exit пропускает хуки atexit, а на них держатся обещания «на выходе
    # процесса»: так закрываются долгоживущие stdio-MCP-серверы (браузер).
    # Аварийный путь зовёт их сам — рядом со своей уборкой.
    cleanup = _Cleanup()
    guard = make_guard(cleanup)

    guard._emergency_exit()

    assert cleanup.done.wait(DEADLINE)
    assert patched.hooked.wait(DEADLINE)
    assert patched.exit_hooks == ["atexit"]
    assert patched.exit.codes == [129]


def test_exit_hooks_run_even_if_cleanup_fails(
    monkeypatch, patched, make_guard
):
    hooked = threading.Event()
    monkeypatch.setattr(threading, "excepthook", lambda args: hooked.set())

    def cleanup() -> None:
        raise RuntimeError("уборка упала")

    guard = make_guard(cleanup)

    guard._emergency_exit()

    assert hooked.wait(DEADLINE)
    assert patched.hooked.wait(DEADLINE)
    assert patched.exit_hooks == ["atexit"]
    assert patched.exit.codes == [129]


def test_force_kill_before_exit_when_cleanup_misses_budget(
    monkeypatch, patched, make_guard
):
    # Уборка не уложилась в бюджет — последний рубеж добивает демонов ДО os._exit,
    # не дожидаясь её: гарантия «сирот нет» не зависит от того, что в уборке зависло.
    events: list[object] = []
    monkeypatch.setattr(
        terminal_guard.os, "_exit", lambda code: events.append(("exit", code))
    )
    release = threading.Event()
    guard = make_guard(
        lambda: release.wait(), force_kill=lambda: events.append("force")
    )
    try:
        guard._emergency_exit()
        assert events == ["force", ("exit", 129)]
    finally:
        release.set()
        patched.hooked.wait(DEADLINE)


def test_force_kill_when_cleanup_fails(monkeypatch, patched, make_guard):
    # Упавшая уборка могла не погасить демонов — добиваем.
    monkeypatch.setattr(threading, "excepthook", lambda args: None)
    forced: list[bool] = []

    def cleanup() -> None:
        raise RuntimeError("уборка упала")

    guard = make_guard(cleanup, force_kill=lambda: forced.append(True))

    guard._emergency_exit()

    assert forced == [True]
    assert patched.exit.codes == [129]
    assert patched.hooked.wait(DEADLINE)


def test_no_force_kill_when_cleanup_finished(patched, make_guard):
    forced: list[bool] = []
    guard = make_guard(_Cleanup(), force_kill=lambda: forced.append(True))

    guard._emergency_exit()

    assert forced == []
    assert patched.exit.codes == [129]
    assert patched.hooked.wait(DEADLINE)


def test_close_force_kills_when_cleanup_fails(make_guard):
    # Штатный выход — та же гарантия: упала уборка → добиваем, ошибка летит дальше.
    forced: list[bool] = []

    def cleanup() -> None:
        raise RuntimeError("уборка упала")

    guard = make_guard(cleanup, force_kill=lambda: forced.append(True))

    with pytest.raises(RuntimeError):
        guard.close()
    guard.close()

    assert forced == [True]


def test_close_does_not_force_kill_after_cleanup(make_guard):
    forced: list[bool] = []
    guard = make_guard(_Cleanup(), force_kill=lambda: forced.append(True))

    guard.close()

    assert forced == []


def test_hanging_cleanup_holds_neither_exit_hooks_nor_exit(
    patched, make_guard
):
    # Уборка и хуки идут рядом, с общим дедлайном: зависшая уборка не съедает
    # время хуков, а выход приходит по бюджету.
    release = threading.Event()
    guard = make_guard(lambda: release.wait())
    try:
        threading.Thread(target=guard._emergency_exit, daemon=True).start()
        assert patched.exit.called.wait(DEADLINE)
        assert patched.exit.codes == [129]
        assert patched.exit_hooks == ["atexit"]
    finally:
        release.set()
        patched.hooked.wait(DEADLINE)


def test_failure_on_emergency_path_still_exits(
    monkeypatch, patched, make_guard
):
    # Исключение аварийного пути (поток не стартовал) не должно молча убить
    # поток-сторож: процесс снова висел бы.
    def silence_output() -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(terminal_guard, "_silence_output", silence_output)
    guard = make_guard(_Cleanup())

    with pytest.raises(RuntimeError):
        guard._emergency_exit()

    assert patched.exit.codes == [129]


def test_silence_failure_does_not_cancel_cleanup(
    monkeypatch, patched, make_guard
):
    # Нет свободного fd на /dev/null — не повод бросить демонов: печать гашения
    # MCP мёртвый терминал терпит сама.
    def silence_output() -> None:
        raise OSError(errno.EMFILE, "too many open files")

    monkeypatch.setattr(terminal_guard, "_silence_output", silence_output)
    cleanup = _Cleanup()
    guard = make_guard(cleanup)

    guard._emergency_exit()

    assert cleanup.done.wait(DEADLINE)
    assert patched.hooked.wait(DEADLINE)
    assert patched.exit.codes == [129]


def test_failing_cleanup_does_not_prevent_exit(
    monkeypatch, patched, make_guard
):
    # Исключение уборки ловим своим excepthook и ждём его: иначе при медленном
    # потоке оно всплыло бы предупреждением уже в чужом тесте.
    raised: list[type[BaseException]] = []
    hooked = threading.Event()

    def excepthook(args) -> None:
        raised.append(args.exc_type)
        hooked.set()

    monkeypatch.setattr(threading, "excepthook", excepthook)

    def cleanup() -> None:
        raise RuntimeError("уборка упала")

    guard = make_guard(cleanup)

    guard._emergency_exit()

    assert patched.exit.codes == [129]
    assert hooked.wait(DEADLINE)
    assert raised == [RuntimeError]
    assert patched.hooked.wait(DEADLINE)


@pytest.mark.parametrize("code", [errno.EIO, errno.ENXIO])
def test_tty_dead_on_eio_and_enxio(monkeypatch, code):
    def write(fd: int, data: bytes) -> int:
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(terminal_guard.os, "write", write)

    assert terminal_guard._tty_alive(2) is False


def test_tty_alive_on_other_write_errors(monkeypatch):
    # Неблокирующий tty, пока в него пишет поток вывода Textual, отвечает EAGAIN —
    # терминал жив, и выход из живого TUI был бы ложным.
    def write(fd: int, data: bytes) -> int:
        raise BlockingIOError(errno.EAGAIN, os.strerror(errno.EAGAIN))

    monkeypatch.setattr(terminal_guard.os, "write", write)

    assert terminal_guard._tty_alive(2) is True


def test_tty_alive_on_successful_write(monkeypatch):
    monkeypatch.setattr(terminal_guard.os, "write", lambda fd, data: len(data))

    assert terminal_guard._tty_alive(2) is True


# --- только POSIX: SIGHUP и tty ----------------------------------------------


@posix_only
def test_sighup_exits_129(patched, make_guard):
    cleanup = _Cleanup()
    guard = make_guard(cleanup)
    guard.install()

    # Без нашего обработчика SIGHUP с диспозицией по умолчанию убил бы pytest.
    assert signal.getsignal(signal.SIGHUP) not in (
        signal.SIG_DFL,
        signal.SIG_IGN,
    )
    signal.raise_signal(signal.SIGHUP)

    assert patched.exit.called.wait(DEADLINE)
    assert patched.exit.codes == [129]
    assert guard.trigger == "signal"
    assert cleanup.done.wait(DEADLINE)
    assert cleanup.count == 1
    assert patched.hooked.wait(DEADLINE)


@posix_only
def test_dead_tty_exits_129(monkeypatch, patched, make_guard):
    monkeypatch.setattr(terminal_guard.os, "isatty", lambda fd: True)
    monkeypatch.setattr(terminal_guard, "_tty_alive", lambda fd: False)
    guard = make_guard(_Cleanup())
    guard.install()

    assert patched.exit.called.wait(DEADLINE)
    assert patched.exit.codes == [129]
    assert guard.trigger == "tty"
    assert patched.hooked.wait(DEADLINE)


@posix_only
def test_output_terminal_is_what_is_polled(monkeypatch, patched, make_guard):
    # Зависание начинается с вывода: Textual рисует в stderr, и именно запись туда
    # убивает его поток вывода. Поэтому сторож следит за терминалом на fd 2.
    polled: list[int] = []

    def tty_alive(fd: int) -> bool:
        polled.append(fd)
        return False

    monkeypatch.setattr(terminal_guard.os, "isatty", lambda fd: fd == 2)
    monkeypatch.setattr(terminal_guard, "_tty_alive", tty_alive)
    guard = make_guard(_Cleanup())
    guard.install()

    assert patched.exit.called.wait(DEADLINE)
    assert guard.trigger == "tty"
    assert set(polled) == {2}
    assert patched.hooked.wait(DEADLINE)


@posix_only
def test_output_not_tty_is_not_polled(monkeypatch, patched, make_guard):
    polled: list[int] = []

    def tty_alive(fd: int) -> bool:
        polled.append(fd)
        return False

    monkeypatch.setattr(terminal_guard.os, "isatty", lambda fd: fd != 2)
    monkeypatch.setattr(terminal_guard, "_tty_alive", tty_alive)
    guard = make_guard(_Cleanup())
    guard.install()

    assert not patched.exit.called.wait(QUIET)
    assert polled == []


@posix_only
def test_tty_alive_sees_hung_up_pty():
    # Настоящий pty: пока master открыт — терминал жив; закрыт master — мёртв.
    # На macOS повешенный tty ещё отвечает на tcgetattr и isatty (проверено на
    # Маке), а запись в него падает с EIO на обеих ОС — признак держится на ней.
    master, slave = os.openpty()
    try:
        assert terminal_guard._tty_alive(slave) is True
        os.close(master)
        master = -1
        assert terminal_guard._tty_alive(slave) is False
    finally:
        if master != -1:
            os.close(master)
        os.close(slave)


@posix_only
def test_live_tty_does_not_exit(monkeypatch, patched, make_guard):
    polled = threading.Event()

    def tty_alive(fd: int) -> bool:
        polled.set()
        return True

    monkeypatch.setattr(terminal_guard.os, "isatty", lambda fd: True)
    monkeypatch.setattr(terminal_guard, "_tty_alive", tty_alive)
    guard = make_guard(_Cleanup())
    guard.install()

    assert polled.wait(DEADLINE)
    assert not patched.exit.called.wait(QUIET)


@posix_only
def test_close_restores_previous_handler_and_wakeup_fd(make_guard):
    # Прежнее состояние — заведомо не дефолтное: SIG_IGN и свой wakeup fd.
    # Иначе «вернул прежнее» неотличимо от «сбросил в дефолт».
    outer_r, outer_w = os.pipe()
    os.set_blocking(outer_w, False)
    previous_hup = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    previous_wakeup = signal.set_wakeup_fd(outer_w)
    guard = make_guard(_Cleanup())
    try:
        guard.install()
        watchers = _guard_threads()
        assert len(watchers) == 1

        guard.close()

        assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
        assert signal.set_wakeup_fd(previous_wakeup) == outer_w
        assert not watchers[0].is_alive()
    finally:
        guard.close()  # повторный close безвреден; нужен, если тест упал до своего
        signal.set_wakeup_fd(previous_wakeup)
        signal.signal(signal.SIGHUP, previous_hup)
        os.close(outer_r)
        os.close(outer_w)


@posix_only
def test_close_restores_signals_even_if_cleanup_fails(make_guard):
    outer_r, outer_w = os.pipe()
    os.set_blocking(outer_w, False)
    previous_hup = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    previous_wakeup = signal.set_wakeup_fd(outer_w)

    def cleanup() -> None:
        raise RuntimeError("уборка упала")

    guard = make_guard(cleanup)
    try:
        guard.install()
        watchers = _guard_threads()
        assert len(watchers) == 1

        with pytest.raises(RuntimeError):
            guard.close()

        assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
        assert signal.set_wakeup_fd(previous_wakeup) == outer_w
        assert not watchers[0].is_alive()
    finally:
        signal.set_wakeup_fd(previous_wakeup)
        signal.signal(signal.SIGHUP, previous_hup)
        os.close(outer_r)
        os.close(outer_w)


@posix_only
def test_cleanup_runs_once_across_sighup_and_close(patched, make_guard):
    cleanup = _Cleanup()
    guard = make_guard(cleanup)
    guard.install()

    # Без нашего обработчика SIGHUP с диспозицией по умолчанию убил бы pytest.
    assert signal.getsignal(signal.SIGHUP) not in (
        signal.SIG_DFL,
        signal.SIG_IGN,
    )
    signal.raise_signal(signal.SIGHUP)
    assert patched.exit.called.wait(DEADLINE)
    assert cleanup.done.wait(DEADLINE)
    assert patched.hooked.wait(DEADLINE)

    guard.close()

    assert cleanup.count == 1
