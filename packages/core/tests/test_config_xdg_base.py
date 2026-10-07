"""GC_CONFIG_DIR/config.yml as a base tier between --config and the package default."""

import os

import pytest
from gc_core.config_modules import env_vars
from gc_core.config_modules.env_vars import EnvVars

_XDG_CFG = """
backend:
  api_mode: chat_completions
  completions_url: "http://127.0.0.1:8014/v1/chat/completions"
agent:
  model: XdgModel
  max_steps: 111
"""

_PKG_CFG = """
backend:
  api_mode: chat_completions
  completions_url: "http://127.0.0.1:8014/v1/chat/completions"
agent:
  model: PkgModel
  max_steps: 222
"""

_EXPLICIT_CFG = """
backend:
  api_mode: chat_completions
  completions_url: "http://127.0.0.1:8014/v1/chat/completions"
agent:
  model: ExplicitModel
  max_steps: 333
"""


@pytest.fixture
def env_with_config_dir(monkeypatch, tmp_path):
    """Fake EnvVars whose gc_paths()['config'] == tmp_path/'xdgcfg' (absolute)."""
    monkeypatch.setattr("sys.platform", "linux")
    xdg_cfg_dir = tmp_path / "xdgcfg"
    xdg_cfg_dir.mkdir()
    fake = EnvVars(gc_config_dir=xdg_cfg_dir)
    monkeypatch.setattr(env_vars, "env", fake)
    return xdg_cfg_dir


@pytest.fixture
def fake_pkg_default(monkeypatch, tmp_path):
    """Monkeypatch _default_config_path() to a controlled tmp 'package' config."""
    from gc_core import config

    pkg = tmp_path / "pkg_config.yml"
    pkg.write_text(_PKG_CFG, encoding="utf-8")
    monkeypatch.setattr(config, "_default_config_path", lambda: str(pkg))
    return pkg


@pytest.fixture
def isolated_cwd(tmp_path, monkeypatch):
    """cwd with no <cwd>/.gc/config.yml unless a test writes one."""
    proj = tmp_path / "project"
    proj.mkdir()
    monkeypatch.chdir(proj)
    return proj


def test_xdg_config_path_returns_path_when_file_exists(env_with_config_dir):
    from gc_core.config import _xdg_config_path

    cfg = env_with_config_dir / "config.yml"
    cfg.write_text(_XDG_CFG, encoding="utf-8")
    result = _xdg_config_path()
    assert result is not None
    assert os.path.abspath(result) == str(cfg.resolve())


def test_xdg_config_path_none_when_missing(env_with_config_dir):
    from gc_core.config import _xdg_config_path

    # No config.yml in the xdg config dir.
    assert _xdg_config_path() is None


def test_xdg_base_used_when_no_explicit_config(
    env_with_config_dir, fake_pkg_default, isolated_cwd
):
    from gc_core.config import load_config

    (env_with_config_dir / "config.yml").write_text(_XDG_CFG, encoding="utf-8")
    cfg = load_config()  # no --config
    assert cfg["model"] == "XdgModel"
    assert cfg["max_steps"] == 111


def test_explicit_config_wins_over_xdg(
    env_with_config_dir, fake_pkg_default, isolated_cwd, tmp_path
):
    from gc_core.config import load_config

    (env_with_config_dir / "config.yml").write_text(_XDG_CFG, encoding="utf-8")
    explicit = tmp_path / "explicit.yml"
    explicit.write_text(_EXPLICIT_CFG, encoding="utf-8")
    cfg = load_config(str(explicit))
    assert cfg["model"] == "ExplicitModel"
    assert cfg["max_steps"] == 333


def test_falls_back_to_package_when_no_xdg_file(
    env_with_config_dir, fake_pkg_default, isolated_cwd
):
    from gc_core.config import load_config

    # gc_config_dir is set but no config.yml inside -> tier 2 skipped.
    cfg = load_config()
    assert cfg["model"] == "PkgModel"
    assert cfg["max_steps"] == 222


def test_no_env_override_uses_package_default(
    monkeypatch, fake_pkg_default, isolated_cwd, tmp_path
):
    """No discoverable XDG config.yml -> back-compat: package default base.

    gc_config_dir points at an empty tmp dir (no config.yml) so the lookup is
    filesystem-neutral and does not reach the real ~/.config/gc/.
    """
    for _k in (
        "GC_HOME",
        "GC_STATE_DIR",
        "GC_CONFIG_DIR",
        "GC_CACHE_DIR",
        "GC_DATA_DIR",
        "XDG_STATE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
    ):
        monkeypatch.delenv(_k, raising=False)
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(env_vars, "env", EnvVars(gc_config_dir=tmp_path))
    from gc_core.config import load_config

    cfg = load_config()
    assert cfg["model"] == "PkgModel"


def test_project_local_merges_over_xdg_base(
    env_with_config_dir, fake_pkg_default, isolated_cwd
):
    from gc_core.config import load_config

    (env_with_config_dir / "config.yml").write_text(_XDG_CFG, encoding="utf-8")
    gc_dir = isolated_cwd / ".gc"
    gc_dir.mkdir()
    (gc_dir / "config.yml").write_text(
        "agent:\n  max_steps: 999\n", encoding="utf-8"
    )
    cfg = load_config()
    # project-local deep-merged on top of the XDG base (not the package).
    assert cfg["max_steps"] == 999
    assert cfg["model"] == "XdgModel"


def test_relative_paths_resolve_from_xdg_dir(
    env_with_config_dir, fake_pkg_default, isolated_cwd
):
    from gc_core.config import load_config

    (env_with_config_dir / "config.yml").write_text(
        _XDG_CFG + "paths:\n  agent_md_path: foo.md\n", encoding="utf-8"
    )
    cfg = load_config()
    # config_dir == winning base dir (the xdg dir), like --config semantics.
    assert cfg["agent_md_path"] == str(env_with_config_dir / "foo.md")
