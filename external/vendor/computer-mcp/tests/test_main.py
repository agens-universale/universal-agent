import subprocess
import sys


def test_help_exits_zero():
    p = subprocess.run(
        [sys.executable, "-m", "computer_mcp", "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert p.returncode == 0
    assert "computer-mcp" in (p.stdout + p.stderr).lower()


def test_version_exits_zero():
    p = subprocess.run(
        [sys.executable, "-m", "computer_mcp", "--version"],
        capture_output=True, text=True, timeout=30,
    )
    assert p.returncode == 0
