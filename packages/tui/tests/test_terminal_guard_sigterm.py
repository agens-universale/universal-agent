"""SIGTERM у gc-ua — тем же путём сторожа терминала, что SIGHUP (спека §5.12).

Сторож слушает переданные ему сигналы одним wakeup fd и выходит с уборкой
кодом 128 + номер сигнала; ``close`` возвращает прежние обработчики всех
своих сигналов. ``main`` отдаёт сторожу SIGHUP и SIGTERM. Выход сторожа —
``os._exit``, поэтому сигналы проверяются в отдельном процессе.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from gc_testkit.paths import package_src_dirs

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="SIGHUP и сигналы сторожа — только POSIX"
)

#: Срок выхода процесса со сторожем после сигнала.
_EXIT_SEC = 10.0

#: Процесс со сторожем SIGHUP и SIGTERM; уборка пишет файл-метку.
_GUARDED = """
import os, signal, sys, time
from gc_tui.services.terminal_guard import TerminalGuard

work = sys.argv[1]

def cleanup():
    open(os.path.join(work, "cleanup"), "w").close()

guard = TerminalGuard(cleanup, signals=(signal.SIGHUP, signal.SIGTERM))
guard.install()
print("ready", flush=True)
try:
    time.sleep(60)
finally:
    guard.close()
"""


def _exit_code_after(signum: int, work: Path) -> int:
    """Код выхода процесса со сторожем после сигнала ``signum``."""
    sources = [str(path) for path in package_src_dirs()]
    if os.environ.get("PYTHONPATH"):
        sources.append(os.environ["PYTHONPATH"])
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sources))
    proc = subprocess.Popen(
        [sys.executable, "-c", _GUARDED, str(work)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    try:
        assert proc.stdout.readline().strip() == b"ready"
        proc.send_signal(signum)
        return proc.wait(_EXIT_SEC)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(_EXIT_SEC)
        proc.stdout.close()


@posix_only
def test_guard_handles_sigterm_with_code_143(tmp_path):
    assert _exit_code_after(signal.SIGTERM, tmp_path) == 143
    assert (tmp_path / "cleanup").exists()


@posix_only
def test_sighup_code_is_still_129(tmp_path):
    assert _exit_code_after(signal.SIGHUP, tmp_path) == 129
    assert (tmp_path / "cleanup").exists()


@posix_only
def test_guard_close_restores_all_signals():
    from gc_tui.services.terminal_guard import TerminalGuard

    def on_hup(signum, frame) -> None:
        del signum, frame

    def on_term(signum, frame) -> None:
        del signum, frame

    before_hup = signal.signal(signal.SIGHUP, on_hup)
    before_term = signal.signal(signal.SIGTERM, on_term)
    try:
        guard = TerminalGuard(
            lambda: None, signals=(signal.SIGHUP, signal.SIGTERM)
        )
        guard.install()
        assert signal.getsignal(signal.SIGTERM) is not on_term
        guard.close()

        assert signal.getsignal(signal.SIGHUP) is on_hup
        assert signal.getsignal(signal.SIGTERM) is on_term
    finally:
        signal.signal(signal.SIGHUP, before_hup)
        signal.signal(signal.SIGTERM, before_term)


@posix_only
def test_main_passes_sigterm_to_guard(monkeypatch, tmp_path):
    pytest.importorskip("textual")

    from gc_tui import app as tui_app
    from gc_tui.services import terminal_guard
    from test_main_wiring import _cfg_for_main

    given: list = []

    class _SpyGuard(terminal_guard.TerminalGuard):
        def __init__(self, cleanup, **kw):
            super().__init__(cleanup, **kw)
            given.append(kw.get("signals"))

    class _FakeApp:
        def __init__(self, **kw):
            pass

        def set_config_path(self, path):
            self._config_path = path

        def _maybe_notice_profile_activation(self, *a, **k):
            pass

        def run(self):
            return None

    monkeypatch.setattr(terminal_guard, "TerminalGuard", _SpyGuard)
    monkeypatch.setattr(tui_app, "TuiApp", _FakeApp)

    from gc_tui import main

    argv = ["--config", str(_cfg_for_main(tmp_path)), "--no-auto-mcp"]
    assert main(argv=argv) == 0
    assert given == [(signal.SIGHUP, signal.SIGTERM)]
