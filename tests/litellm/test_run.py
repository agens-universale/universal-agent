"""Tests for litellm/run.py: callback import sanity, command build."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "litellm"))

import run as run_mod  # type: ignore[import-not-found]


def test_build_command_uses_litellm_cli() -> None:
    cmd = run_mod.build_command(port=4000, cli="/fake/litellm")
    assert cmd[0] == "/fake/litellm"
    assert "--config" in cmd
    assert "proxy_config.yaml" in cmd
    assert "--port" in cmd
    assert "4000" in cmd


def test_build_command_uses_given_port() -> None:
    cmd = run_mod.build_command(port=9999, cli="/fake/litellm")
    assert "9999" in cmd


def test_check_callback_importable_true_for_real_layout() -> None:
    real_litellm = Path(__file__).resolve().parents[2] / "litellm"
    assert run_mod.check_callback_importable(real_litellm) is True


def test_check_callback_importable_false_for_empty_dir(tmp_path: Path) -> None:
    assert run_mod.check_callback_importable(tmp_path) is False
