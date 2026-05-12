"""Tests for litellm/install.py: dependency check + .env.example bootstrap."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Make litellm/install.py importable as a module (no package dunder for litellm/).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "litellm"))

import install as install_mod  # type: ignore[import-not-found]


def test_check_litellm_installed_returns_true_when_importable() -> None:
    with patch.object(install_mod.importlib, "import_module") as imp:
        assert install_mod.check_litellm_installed() is True
        imp.assert_called_with("litellm")


def test_check_litellm_installed_returns_false_on_importerror() -> None:
    with patch.object(install_mod.importlib, "import_module", side_effect=ImportError):
        assert install_mod.check_litellm_installed() is False


def test_seed_env_file_copies_when_missing(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text("GIGACHAT_CREDENTIALS=\n")
    created = install_mod.seed_env_file(tmp_path)
    assert created is True
    assert (tmp_path / ".env").read_text() == "GIGACHAT_CREDENTIALS=\n"


def test_seed_env_file_noop_when_exists(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text("X=1\n")
    (tmp_path / ".env").write_text("Y=2\n")
    created = install_mod.seed_env_file(tmp_path)
    assert created is False
    assert (tmp_path / ".env").read_text() == "Y=2\n"


def test_seed_env_file_raises_when_template_missing(tmp_path: Path) -> None:
    with pytest.raises(install_mod.InstallError, match="missing"):
        install_mod.seed_env_file(tmp_path)
