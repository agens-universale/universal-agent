import subprocess
import sys


def test_help_exits_zero():
    p = subprocess.run(
        [sys.executable, "-m", "gc_computer_mcp", "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert p.returncode == 0
    assert "gc-computer-mcp" in (p.stdout + p.stderr).lower()


def test_version_exits_zero():
    p = subprocess.run(
        [sys.executable, "-m", "gc_computer_mcp", "--version"],
        capture_output=True, text=True, timeout=30,
    )
    assert p.returncode == 0
