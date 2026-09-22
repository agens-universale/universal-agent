"""os_ops.write_clipboard — нативная запись в системный буфер (дополняет OSC 52)."""

import sys
from types import SimpleNamespace

import pytest
from gc_tui.services import os_ops


def _ok_run(captured):
    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["input"] = kw.get("input")
        return SimpleNamespace(returncode=0)

    return fake_run


def test_write_clipboard_darwin(monkeypatch):
    monkeypatch.setattr(os_ops.sys, "platform", "darwin")
    cap: dict = {}
    monkeypatch.setattr(os_ops.subprocess, "run", _ok_run(cap))
    assert os_ops.write_clipboard("привет") is True
    assert cap["cmd"] == ["pbcopy"]
    assert cap["input"] == "привет".encode()


def test_write_clipboard_windows_uses_winapi(monkeypatch):
    # Win32 — через ctypes (_win_write_clipboard), без subprocess.
    monkeypatch.setattr(os_ops.sys, "platform", "win32")
    seen: dict = {}
    monkeypatch.setattr(os_ops, "_win_write_clipboard", lambda t: seen.update(text=t) or True)
    assert os_ops.write_clipboard("тест") is True
    assert seen["text"] == "тест"


def test_write_clipboard_linux_falls_through_tools(monkeypatch):
    monkeypatch.setattr(os_ops.sys, "platform", "linux")
    seen: list = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return SimpleNamespace(returncode=0 if cmd[0] == "xclip" else 1)

    monkeypatch.setattr(os_ops.subprocess, "run", fake_run)
    assert os_ops.write_clipboard("x") is True
    assert seen[0] == ["wl-copy"]
    assert ["xclip", "-selection", "clipboard", "-i", "-loops", "1"] in seen


def test_write_clipboard_graceful_on_missing_tool(monkeypatch):
    monkeypatch.setattr(os_ops.sys, "platform", "darwin")

    def boom(cmd, **kw):
        raise FileNotFoundError

    monkeypatch.setattr(os_ops.subprocess, "run", boom)
    assert os_ops.write_clipboard("x") is False  # не падает, тихо False


# --- реальный round-trip через Win32 API (прогоняется только на Windows) ---


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 clipboard API")
def test_win_clipboard_roundtrip_cyrillic_and_glyphs():
    saved = os_ops.read_clipboard()
    try:
        s = "привет ● ✓ ⠇ мир"  # кириллица + текстовые глифы (проверка CF_UNICODETEXT/UTF-16)
        assert os_ops.write_clipboard(s) is True
        assert os_ops.read_clipboard() == s
    finally:
        os_ops.write_clipboard(saved)  # вернуть прежний текст буфера
