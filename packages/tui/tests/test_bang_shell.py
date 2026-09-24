"""Юнит-тесты bang-shell: парсинг, preflight, truncate, reject, нормализация."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest
from gc_tui.services.bang_shell import (
    BangShellController,
    BangShellSettings,
    ClearMeta,
    normalize_output,
    parse_body,
    parse_clear_meta,
    preflight,
    truncate_output,
)


def test_parse_body_strips_bang_and_optional_space():
    assert parse_body("!git status") == "git status"
    assert parse_body("! git status") == "git status"
    assert parse_body("!  echo hi") == "echo hi"


def test_parse_body_empty():
    assert parse_body("!") is None
    assert parse_body("!   ") is None
    assert parse_body("!") is None


def test_parse_body_double_bang_is_command():
    assert parse_body("!!foo") == "!foo"


def test_parse_body_multiline_kept():
    assert parse_body("!echo a\necho b") == "echo a\necho b"


def test_parse_clear_meta_all():
    meta, err = parse_clear_meta("clear")
    assert err is None
    assert meta == ClearMeta(count=None)
    meta, err = parse_clear_meta("CLEAR")
    assert err is None and meta == ClearMeta(count=None)


def test_parse_clear_meta_n():
    meta, err = parse_clear_meta("clear 3")
    assert err is None and meta == ClearMeta(count=3)
    meta, err = parse_clear_meta("clear -2")
    assert err is None and meta == ClearMeta(count=2)


def test_parse_clear_meta_not_clear():
    assert parse_clear_meta("echo clear") == (None, None)
    assert parse_clear_meta("git status") == (None, None)


def test_parse_clear_meta_invalid():
    meta, err = parse_clear_meta("clear x")
    assert meta is None and err is not None
    meta, err = parse_clear_meta("clear 0")
    assert meta is None and err is not None
    meta, err = parse_clear_meta("clear 1 2")
    assert meta is None and err is not None


def test_parse_clear_meta_rejects_a_superscript_digit():
    """Класс «isdigit + int»: `²` проходит `isdigit()`, а `int("²")` падает
    голым ValueError — `! clear ²` роняло разбор строки ввода вместо честного
    «использование: ! clear [N]». Минус перед ней бьёт в ту же пару: знак
    снимается ДО проверки, и `-²` доезжает до того же `int()`."""
    assert parse_clear_meta("clear ²") == (None, "использование: ! clear [N]")
    assert parse_clear_meta("clear -²") == (None, "использование: ! clear [N]")


def test_normalize_crlf():
    assert normalize_output("a\r\nb\r\n") == "a\nb\n"
    assert normalize_output("a\nb") == "a\nb"


def test_truncate_by_bytes():
    out = truncate_output("abcdefghij", max_bytes=5, max_lines=100)
    assert out.startswith("abcde")
    assert "truncated" in out
    assert "bytes" in out


def test_truncate_by_lines():
    text = "\n".join(f"L{i}" for i in range(10))
    out = truncate_output(text, max_bytes=10_000, max_lines=3)
    assert "L0" in out and "L2" in out
    assert "L3" not in out
    assert "truncated" in out
    assert "lines" in out


def test_truncate_noop_when_under_limits():
    assert truncate_output("ok\n", max_bytes=100, max_lines=10) == "ok\n"


def test_truncate_binary_nul_heavy():
    raw = "x\x00\x00\x00\x00y"
    out = truncate_output(raw, max_bytes=1000, max_lines=100)
    assert "binary" in out.lower()


@pytest.mark.parametrize(
    "cmd",
    [
        "vim file",
        "nvim x",
        "vi",
        "nano",
        "emacs",
        "less f",
        "more f",
        "top",
        "htop",
        "btop",
        "watch ls",
        "/usr/bin/vim x",
        "env FOO=1 vim x",
        "FOO=1 nano x",
    ],
)
def test_preflight_rejects_interactive(cmd):
    reason = preflight(cmd)
    assert reason is not None
    assert "интерактив" in reason.lower()


@pytest.mark.parametrize(
    "cmd",
    [
        "docker run -it alpine sh",
        "docker run -ti alpine",
        "podman run --tty img",
        "nerdctl run --interactive img",
    ],
)
def test_preflight_rejects_container_tty(cmd):
    reason = preflight(cmd)
    assert reason is not None
    assert "интерактив" in reason.lower()


@pytest.mark.parametrize(
    "cmd",
    [
        "sudo id",
        "sudo -n id",
        "su",
        "doas id",
        "pkexec id",
        "runas /user:Admin cmd",
        "env FOO=1 sudo -n id",
    ],
)
def test_preflight_rejects_privilege(cmd):
    reason = preflight(cmd)
    assert reason is not None
    assert "привилег" in reason.lower()


@pytest.mark.parametrize(
    "cmd",
    ["echo hello", "git status", "docker ps", "ls -la"],
)
def test_preflight_allows_normal(cmd):
    assert preflight(cmd) is None


def test_controller_rejects_when_disabled():
    ctl = BangShellController()
    result = ctl.run(
        "echo hi", cwd=".", settings=BangShellSettings(enabled=False)
    )
    assert result.rejected is not None
    assert "выключен" in result.rejected
    assert result.exit_code is None


def test_controller_rejects_empty():
    ctl = BangShellController()
    result = ctl.run("", cwd=".", settings=BangShellSettings())
    assert result.rejected is not None
    assert "пустая" in result.rejected.lower()


def test_controller_rejects_concurrent():
    ctl = BangShellController()
    ctl._running = True  # simulate in-flight
    result = ctl.run("echo hi", cwd=".", settings=BangShellSettings())
    assert result.rejected is not None
    assert "дождитесь" in result.rejected.lower()


def test_controller_preflight_no_spawn():
    ctl = BangShellController()
    with patch("gc_tui.services.bang_shell.subprocess.Popen") as popen:
        result = ctl.run("vim x", cwd=".", settings=BangShellSettings())
    popen.assert_not_called()
    assert result.rejected is not None
    assert result.command == "vim x"


def test_controller_run_success_mocked():
    ctl = BangShellController()
    proc = MagicMock()
    proc.stdout = MagicMock()
    proc.stdout.read.return_value = b"hello\n"
    proc.wait.return_value = 0
    proc.poll.return_value = 0
    proc.pid = 1234

    with patch(
        "gc_tui.services.bang_shell.subprocess.Popen", return_value=proc
    ) as popen:
        with patch("gc_tui.services.bang_shell.preserve_console_mode"):
            result = ctl.run(
                "echo hello", cwd="/tmp", settings=BangShellSettings()
            )

    popen.assert_called_once()
    kwargs = popen.call_args.kwargs
    assert kwargs["shell"] is True
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.STDOUT
    assert result.output == "hello\n"
    assert result.exit_code == 0
    assert not result.cancelled
    assert result.rejected is None


def test_controller_normalizes_crlf_in_output():
    ctl = BangShellController()
    proc = MagicMock()
    proc.stdout = MagicMock()
    proc.stdout.read.return_value = b"a\r\nb\r\n"
    proc.wait.return_value = 0
    proc.poll.return_value = 0
    proc.pid = 1

    with patch(
        "gc_tui.services.bang_shell.subprocess.Popen", return_value=proc
    ):
        with patch("gc_tui.services.bang_shell.preserve_console_mode"):
            result = ctl.run("echo", cwd=".", settings=BangShellSettings())

    assert result.output == "a\nb\n"


def test_controller_cancel_sets_flag():
    ctl = BangShellController()
    proc = MagicMock()
    proc.stdout = MagicMock()

    def _slow_read():
        ctl.cancel()
        return b""

    proc.stdout.read.side_effect = _slow_read
    proc.wait.return_value = -9
    proc.poll.return_value = None
    proc.pid = 42

    with patch(
        "gc_tui.services.bang_shell.subprocess.Popen", return_value=proc
    ):
        with patch("gc_tui.services.bang_shell.preserve_console_mode"):
            with patch("gc_tui.services.bang_shell._kill_process"):
                result = ctl.run(
                    "sleep 99", cwd=".", settings=BangShellSettings()
                )

    assert result.cancelled


def test_controller_real_echo():
    """Smoke: реальный shell=True echo (POSIX/Windows)."""
    ctl = BangShellController()
    result = ctl.run("echo hello", cwd=".", settings=BangShellSettings())
    assert result.rejected is None
    assert result.exit_code == 0
    assert "hello" in result.output


def test_settings_from_mapping():
    s = BangShellSettings.from_mapping(
        {
            "enabled": False,
            "timeout_sec": 5,
            "max_output_bytes": 10,
            "max_output_lines": 2,
        }
    )
    assert s.enabled is False
    assert s.timeout_sec == 5
    assert s.max_output_bytes == 10
    assert s.max_output_lines == 2


def test_settings_from_mapping_defaults():
    s = BangShellSettings.from_mapping(None)
    assert s.enabled is True
    assert s.timeout_sec is None
    assert s.max_output_bytes == 32768
    assert s.max_output_lines == 200
