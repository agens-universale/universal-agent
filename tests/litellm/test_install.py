"""Tests for litellm/install.py: pip install + proxy_config.yaml bootstrap."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make litellm/install.py importable as a module (no package dunder for litellm/).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "litellm"))

import install as install_mod  # type: ignore[import-not-found]


def test_installed_litellm_version_returns_version_when_importable() -> None:
    with patch.object(install_mod.importlib, "import_module"), patch.object(
        install_mod.importlib.metadata, "version", return_value="1.83.14"
    ):
        assert install_mod.installed_litellm_version() == "1.83.14"


def test_installed_litellm_version_returns_none_on_importerror() -> None:
    with patch.object(install_mod.importlib, "import_module", side_effect=ImportError):
        assert install_mod.installed_litellm_version() is None


def test_ensure_litellm_installed_skips_when_version_matches() -> None:
    with patch.object(
        install_mod, "installed_litellm_version", return_value="1.83.14"
    ), patch.object(install_mod, "pip_install_litellm") as pip:
        invoked = install_mod.ensure_litellm_installed()
        assert invoked is False
        pip.assert_not_called()


def test_ensure_litellm_installed_runs_pip_when_missing() -> None:
    with patch.object(
        install_mod, "installed_litellm_version", return_value=None
    ), patch.object(install_mod, "pip_install_litellm") as pip:
        invoked = install_mod.ensure_litellm_installed()
        assert invoked is True
        pip.assert_called_once_with(install_mod.LITELLM_PIN)


def test_ensure_litellm_installed_runs_pip_on_version_mismatch() -> None:
    with patch.object(
        install_mod, "installed_litellm_version", return_value="1.80.0"
    ), patch.object(install_mod, "pip_install_litellm") as pip:
        invoked = install_mod.ensure_litellm_installed()
        assert invoked is True
        pip.assert_called_once()


def test_pip_install_litellm_raises_on_nonzero_exit() -> None:
    fake = MagicMock(returncode=1)
    with patch.object(install_mod.subprocess, "run", return_value=fake):
        with pytest.raises(install_mod.InstallError, match="pip install failed"):
            install_mod.pip_install_litellm("litellm[proxy]==1.83.14")


def test_seed_proxy_config_copies_when_missing(tmp_path: Path) -> None:
    (tmp_path / "proxy_config.example.yaml").write_text("model_list: []\n")
    created = install_mod.seed_proxy_config(tmp_path)
    assert created is True
    assert (tmp_path / "proxy_config.yaml").read_text() == "model_list: []\n"


def test_seed_proxy_config_noop_when_exists(tmp_path: Path) -> None:
    (tmp_path / "proxy_config.example.yaml").write_text("template\n")
    (tmp_path / "proxy_config.yaml").write_text("user-edited\n")
    created = install_mod.seed_proxy_config(tmp_path)
    assert created is False
    assert (tmp_path / "proxy_config.yaml").read_text() == "user-edited\n"


def test_seed_proxy_config_raises_when_template_missing(tmp_path: Path) -> None:
    with pytest.raises(install_mod.InstallError, match="missing"):
        install_mod.seed_proxy_config(tmp_path)
