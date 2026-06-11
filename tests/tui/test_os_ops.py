"""os_ops: macOS/Linux/Windows адаптер."""
from unittest.mock import patch, MagicMock

from tui.services.os_ops import read_clipboard


def test_read_clipboard_windows_uses_get_clipboard(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    result = MagicMock()
    result.stdout = "hello\r\n".encode("utf-8")
    with patch("subprocess.run", return_value=result) as mock_run:
        text = read_clipboard()
        cmd = mock_run.call_args.args[0]
        assert cmd[0] == "powershell"
        # UTF-8 форсируется перед Get-Clipboard, иначе кириллица ломается.
        assert "Get-Clipboard -Raw" in cmd[-1]
        assert "UTF8" in cmd[-1]
    assert text == "hello"


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
