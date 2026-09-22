"""os_ops: macOS/Linux/Windows адаптер."""
from unittest.mock import MagicMock, patch

from gc_tui.services import os_ops
from gc_tui.services.os_ops import read_clipboard


def test_read_clipboard_windows_uses_winapi(monkeypatch):
    # Win32 — через ctypes (_win_read_clipboard), без subprocess: PowerShell сбивал
    # кодовую страницу/мышь консоли. rstrip \r\n сохраняется.
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(os_ops, "_win_read_clipboard", lambda: "hello\r\n")
    assert read_clipboard() == "hello"


def test_read_clipboard_macos_uses_pbpaste(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    result = MagicMock()
    result.stdout = b"copied text"
    with patch("subprocess.run", return_value=result) as mock_run:
        text = read_clipboard()
        assert mock_run.call_args.args[0] == ["pbpaste"]
    assert text == "copied text"


def test_read_clipboard_returns_empty_when_tool_missing(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    with patch("subprocess.run", side_effect=FileNotFoundError):
        assert read_clipboard() == ""
