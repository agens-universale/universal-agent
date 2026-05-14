"""Tests for litellm/run.py: callback import sanity, command build."""
import importlib.metadata
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "litellm"))

import run as run_mod  # type: ignore[import-not-found]


def _litellm_pypi_installed() -> bool:
    """PyPI `litellm` is optional; see litellm/README.md and install.py.

    Do not use ``find_spec("litellm")``: the repo directory ``litellm/`` is on
    ``sys.path`` and registers as a *namespace* shadow, without
    ``litellm.integrations`` — that gives a false positive.
    """
    try:
        importlib.metadata.version("litellm")
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


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


@pytest.mark.skipif(
    not _litellm_pypi_installed(),
    reason="PyPI `litellm` not installed — run `uv run python litellm/install.py` once",
)
def test_check_callback_importable_true_for_real_layout() -> None:
    real_litellm = Path(__file__).resolve().parents[2] / "litellm"
    assert run_mod.check_callback_importable(real_litellm) is True


def test_check_callback_importable_false_for_empty_dir(tmp_path: Path) -> None:
    assert run_mod.check_callback_importable(tmp_path) is False
