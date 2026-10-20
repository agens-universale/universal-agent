"""Проба цвета фона терминала: разбор ответа и обмен на настоящем pty.

Векторы разбора — реальные ответы терминалов (Mac, 2026-10-20): Terminal.app
завершает ответ OSC 11 символом BEL, iTerm2 — ST. Обмен проверяется на pty:
тестовый поток играет роль терминала на master-стороне.
"""

from __future__ import annotations

import io
import os
import select
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from gc_tui.services import terminal_caps
from gc_tui.services.terminal_caps import parse_osc11, query_background

TERMINAL_APP = b"\x1b]11;rgb:213d/2743/33e7\x07"
ITERM2 = b"\x1b]11;rgb:16a5/1900/1e96\x1b\\"
DA1_TERMINAL_APP = b"\x1b[?1;2c"
DA1_ITERM2 = b"\x1b[?64;1;2;4;6;17;18;21;22;52c"

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="только POSIX: нужен настоящий pty"
)


def test_parse_terminal_app_bel():
    assert parse_osc11(TERMINAL_APP + DA1_TERMINAL_APP) == (0x21, 0x27, 0x33)


def test_parse_iterm2_st():
    assert parse_osc11(ITERM2 + DA1_ITERM2) == (0x16, 0x19, 0x1E)


def test_parse_with_garbage_around():
    data = b"xx\x1b[1;1R" + TERMINAL_APP + b"yy" + DA1_TERMINAL_APP
    assert parse_osc11(data) == (0x21, 0x27, 0x33)


def test_parse_da1_only_is_none():
    assert parse_osc11(DA1_TERMINAL_APP) is None


def test_parse_no_osc_is_none():
    assert parse_osc11(b"") is None
    assert parse_osc11(b"\x1b]10;rgb:ffff/ffff/ffff\x07") is None


def test_parse_unterminated_is_none():
    assert parse_osc11(b"\x1b]11;rgb:213d/2743/33e7") is None


def test_parse_short_components_scale_by_length():
    assert parse_osc11(b"\x1b]11;rgb:f/8/0\x07") == (255, 136, 0)
    assert parse_osc11(b"\x1b]11;rgb:ff/80/00\x07") == (255, 128, 0)
    assert parse_osc11(b"\x1b]11;rgb:fff/800/000\x1b\\") == (255, 128, 0)


def test_windows_is_none_without_write(monkeypatch):
    monkeypatch.setattr(terminal_caps.sys, "platform", "win32")
    out = io.BytesIO()
    assert query_background(stdin=io.BytesIO(), stdout=out) is None
    assert out.getvalue() == b""


@posix_only
def test_not_a_tty_is_none_without_write():
    read_fd, write_fd = os.pipe()
    try:
        files = SimpleNamespace(fileno=lambda: write_fd)
        assert query_background(stdin=files, stdout=files) is None
        assert select.select([read_fd], [], [], 0)[0] == []
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.fixture
def pty_pair(monkeypatch):
    """pty в raw-режиме (без эха): slave — «наш» терминал, master — эмулятор."""
    if sys.platform == "win32":
        pytest.skip("только POSIX: нужен настоящий pty")
    import pty
    import tty

    monkeypatch.setenv("TERM", "xterm-256color")
    master, slave = pty.openpty()
    tty.setraw(slave)
    yield master, slave
    os.close(slave)
    os.close(master)


def _tty(fd: int) -> SimpleNamespace:
    return SimpleNamespace(fileno=lambda: fd)


def _emulate(master: int, reply: bytes) -> threading.Thread:
    """Терминал: дождаться запроса DA1 и ответить `reply` (пусто — молчать)."""

    def run() -> None:
        seen = b""
        while b"\x1b[c" not in seen:
            try:
                seen += os.read(master, 1024)
            except OSError:
                return
        if reply:
            os.write(master, reply)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


@pytest.mark.parametrize("term", ["", "dumb"])
def test_dumb_term_is_none_without_write(pty_pair, monkeypatch, term):
    master, slave = pty_pair
    monkeypatch.setenv("TERM", term)
    assert query_background(stdin=_tty(slave), stdout=_tty(slave)) is None
    assert select.select([master], [], [], 0)[0] == []


def test_reply_with_color(pty_pair):
    import termios

    master, slave = pty_pair
    before = termios.tcgetattr(slave)
    _emulate(master, TERMINAL_APP + DA1_TERMINAL_APP)

    rgb = query_background(timeout=2.0, stdin=_tty(slave), stdout=_tty(slave))

    assert rgb == (0x21, 0x27, 0x33)
    assert termios.tcgetattr(slave) == before


def test_iterm2_reply_with_color(pty_pair):
    master, slave = pty_pair
    _emulate(master, ITERM2 + DA1_ITERM2)

    rgb = query_background(timeout=2.0, stdin=_tty(slave), stdout=_tty(slave))

    assert rgb == (0x16, 0x19, 0x1E)


def test_da1_only_is_none_without_waiting(pty_pair):
    master, slave = pty_pair
    _emulate(master, DA1_TERMINAL_APP)

    started = time.monotonic()
    rgb = query_background(timeout=2.0, stdin=_tty(slave), stdout=_tty(slave))

    assert rgb is None
    assert time.monotonic() - started < 1.0


def test_silent_terminal_is_none_by_timeout(pty_pair):
    master, slave = pty_pair
    _emulate(master, b"")

    started = time.monotonic()
    rgb = query_background(timeout=0.1, stdin=_tty(slave), stdout=_tty(slave))
    elapsed = time.monotonic() - started

    assert rgb is None
    assert 0.1 <= elapsed < 1.0


def test_late_reply_is_flushed_from_input(pty_pair, monkeypatch):
    """Ответ пришёл, когда проба уже перестала ждать: он не должен остаться
    во вводе, иначе Textual прочтёт его как нажатия клавиш."""
    master, slave = pty_pair
    _emulate(master, b"")
    real_select = select.select
    fired = []

    def select_then_late_reply(rlist, wlist, xlist, timeout):
        ready = real_select(rlist, wlist, xlist, timeout)
        fired.append(ready)
        if not ready[0]:  # проба дождалась таймаута — тут приходит ответ
            os.write(master, TERMINAL_APP + DA1_TERMINAL_APP)
            assert real_select([slave], [], [], 2.0)[0] == [slave]
        return ready

    monkeypatch.setattr(
        terminal_caps, "select", SimpleNamespace(select=select_then_late_reply)
    )

    rgb = query_background(timeout=0.1, stdin=_tty(slave), stdout=_tty(slave))

    assert rgb is None
    assert any(not ready[0] for ready in fired)
    assert real_select([slave], [], [], 0.05)[0] == []


def test_mode_restored_after_error_in_exchange(pty_pair, monkeypatch):
    import termios

    _, slave = pty_pair
    saved = termios.tcgetattr(slave)
    calls = []
    real_tcsetattr = termios.tcsetattr

    def spy(fd, when, attrs):
        calls.append((when, attrs))
        real_tcsetattr(fd, when, attrs)

    def broken_write(fd, data):
        raise OSError("write failed")

    monkeypatch.setattr(termios, "tcsetattr", spy)
    monkeypatch.setattr(terminal_caps.os, "write", broken_write)

    rgb = query_background(stdin=_tty(slave), stdout=_tty(slave))

    assert rgb is None
    assert calls[-1] == (termios.TCSAFLUSH, saved)


def test_restore_failure_does_not_raise(pty_pair, monkeypatch):
    import termios

    master, slave = pty_pair
    _emulate(master, TERMINAL_APP + DA1_TERMINAL_APP)
    real_tcsetattr = termios.tcsetattr

    def failing_restore(fd, when, attrs):
        if when == termios.TCSAFLUSH:
            raise termios.error(5, "Input/output error")
        real_tcsetattr(fd, when, attrs)

    monkeypatch.setattr(termios, "tcsetattr", failing_restore)

    rgb = query_background(timeout=2.0, stdin=_tty(slave), stdout=_tty(slave))

    assert rgb == (0x21, 0x27, 0x33)


def test_reply_split_across_reads(pty_pair):
    master, slave = pty_pair
    parts = [TERMINAL_APP[:9], TERMINAL_APP[9:] + DA1_TERMINAL_APP[:3]]
    parts.append(DA1_TERMINAL_APP[3:])

    def run() -> None:
        seen = b""
        while b"\x1b[c" not in seen:
            seen += os.read(master, 1024)
        for part in parts:
            os.write(master, part)
            time.sleep(0.05)

    threading.Thread(target=run, daemon=True).start()

    rgb = query_background(timeout=2.0, stdin=_tty(slave), stdout=_tty(slave))

    assert rgb == (0x21, 0x27, 0x33)
