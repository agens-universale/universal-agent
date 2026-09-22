from pathlib import Path

import pytest
from gc_core.config_modules.env_vars import EnvVars


@pytest.fixture
def clear_gc_env(monkeypatch):
    """Очистить env-переменные реестра для гермитичности тестов."""
    for key in (
        "GC_HOME",
        "GC_STATE_DIR",
        "GC_CONFIG_DIR",
        "GC_CACHE_DIR",
        "GC_DATA_DIR",
        "XDG_STATE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "MCP_DEBUG",
        "SERVER_CONFIG_PATH",
        "SWARM_AGENTMD_PATH",
        "SWARM_THREADS_DIR",
        "SWARM_SKILLS_DIR",
    ):
        monkeypatch.delenv(key, raising=False)


def test_envvars_defaults_all_optional_paths_are_none(clear_gc_env):
    e = EnvVars()
    assert e.gc_home is None
    assert e.gc_state_dir is None
    assert e.gc_config_dir is None
    assert e.gc_cache_dir is None
    assert e.gc_data_dir is None


def test_gc_home_empty_string_means_none(clear_gc_env, monkeypatch):
    monkeypatch.setenv("GC_HOME", "")
    e = EnvVars()
    assert e.gc_home is None


def test_gc_home_whitespace_means_none(clear_gc_env, monkeypatch):
    monkeypatch.setenv("GC_HOME", "   ")
    e = EnvVars()
    assert e.gc_home is None


def test_gc_state_dir_empty_means_none(clear_gc_env, monkeypatch):
    monkeypatch.setenv("GC_STATE_DIR", "")
    e = EnvVars()
    assert e.gc_state_dir is None


def test_gc_home_relative_raises(clear_gc_env, monkeypatch):
    monkeypatch.setenv("GC_HOME", "rel/path")
    with pytest.raises(ValueError, match="absolute"):
        EnvVars()


def test_gc_state_dir_relative_raises(clear_gc_env, monkeypatch):
    monkeypatch.setenv("GC_STATE_DIR", "rel/state")
    with pytest.raises(ValueError, match="absolute"):
        EnvVars()


def test_gc_home_absolute_accepted(clear_gc_env, monkeypatch, tmp_path):
    monkeypatch.setenv("GC_HOME", str(tmp_path))
    e = EnvVars()
    assert e.gc_home == tmp_path


def test_mcp_debug_default_false(clear_gc_env):
    e = EnvVars()
    assert e.mcp_debug is False


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("yes", True),
        ("y", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("", False),
        ("garbage", False),
    ],
)
def test_mcp_debug_truthy_strings(clear_gc_env, monkeypatch, raw, expected):
    monkeypatch.setenv("MCP_DEBUG", raw)
    e = EnvVars()
    assert e.mcp_debug is expected


def test_server_config_path_default_none(clear_gc_env):
    e = EnvVars()
    assert e.server_config_path is None


def test_server_config_path_from_env(clear_gc_env, monkeypatch):
    monkeypatch.setenv("SERVER_CONFIG_PATH", "/etc/agent/config.yml")
    e = EnvVars()
    assert e.server_config_path == Path("/etc/agent/config.yml")


def test_server_config_path_empty_means_none(clear_gc_env, monkeypatch):
    monkeypatch.setenv("SERVER_CONFIG_PATH", "")
    e = EnvVars()
    assert e.server_config_path is None


def test_swarm_legacy_default_none(clear_gc_env):
    e = EnvVars()
    assert e.swarm_agentmd_path is None
    assert e.swarm_threads_dir is None
    assert e.swarm_skills_dir is None


def test_swarm_legacy_from_env(clear_gc_env, monkeypatch):
    monkeypatch.setenv("SWARM_AGENTMD_PATH", "/swarm/agent.md")
    monkeypatch.setenv("SWARM_THREADS_DIR", "/swarm/threads")
    monkeypatch.setenv("SWARM_SKILLS_DIR", "/swarm/skills")
    e = EnvVars()
    assert e.swarm_agentmd_path == Path("/swarm/agent.md")
    assert e.swarm_threads_dir == Path("/swarm/threads")
    assert e.swarm_skills_dir == Path("/swarm/skills")


def test_swarm_legacy_empty_means_none(clear_gc_env, monkeypatch):
    monkeypatch.setenv("SWARM_THREADS_DIR", "")
    e = EnvVars()
    assert e.swarm_threads_dir is None


from gc_core.config_modules.env_vars import project_slug


def test_project_slug_windows_path_double_dash():
    assert project_slug("D:\\dev\\sber\\background-agent") == "d--dev-sber-background-agent"


def test_project_slug_posix_path_keeps_leading_dash():
    assert project_slug("/home/dmitry/work") == "-home-dmitry-work"


def test_project_slug_lowercases():
    assert project_slug("/Home/User") == "-home-user"


def test_project_slug_empty_returns_default():
    assert project_slug("") == "default"


def test_project_slug_only_separators_returns_default():
    assert project_slug("///") == "default"


def test_project_slug_uses_cwd_when_no_arg(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    slug = project_slug()
    assert isinstance(slug, str)
    assert len(slug) > 0


from gc_core.config_modules import env_vars as env_vars_module
from gc_core.config_modules.env_vars import gc_paths


def _isolate_env(monkeypatch, **overrides):
    """Чистая EnvVars + monkeypatch sys.platform, чтобы тесты были детерминистичны."""
    fresh = EnvVars(**overrides)
    monkeypatch.setattr(env_vars_module, "env", fresh)
    return fresh


def test_gc_paths_linux_xdg_defaults(clear_gc_env, monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    _isolate_env(monkeypatch)
    paths = gc_paths()
    home = Path.home()
    assert paths["state"] == home / ".local" / "state" / "gc"
    assert paths["config"] == home / ".config" / "gc"
    assert paths["cache"] == home / ".cache" / "gc"
    assert paths["data"] == home / ".local" / "share" / "gc"


def test_gc_paths_macos_xdg_defaults(clear_gc_env, monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    _isolate_env(monkeypatch)
    paths = gc_paths()
    home = Path.home()
    # Mac как Linux (Mac — это Unix; мы сознательно не следуем Apple HIG).
    assert paths["state"] == home / ".local" / "state" / "gc"
    assert paths["config"] == home / ".config" / "gc"
    assert paths["cache"] == home / ".cache" / "gc"
    assert paths["data"] == home / ".local" / "share" / "gc"


def test_gc_paths_windows_dot_gc_defaults(clear_gc_env, monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    _isolate_env(monkeypatch)
    paths = gc_paths()
    home = Path.home()
    gc = home / ".gc"
    assert paths["state"] == gc
    assert paths["config"] == gc
    assert paths["cache"] == gc / "cache"
    assert paths["data"] == gc


def test_gc_paths_xdg_state_home_respected_on_unix(clear_gc_env, monkeypatch, tmp_path):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    _isolate_env(monkeypatch)
    paths = gc_paths()
    assert paths["state"] == tmp_path / "gc"


def test_gc_paths_xdg_ignored_on_windows(clear_gc_env, monkeypatch, tmp_path):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    _isolate_env(monkeypatch)
    paths = gc_paths()
    assert paths["state"] == Path.home() / ".gc"  # XDG ignored на Win


def test_gc_paths_gc_home_collapses_all(clear_gc_env, monkeypatch, tmp_path):
    monkeypatch.setattr("sys.platform", "linux")
    _isolate_env(monkeypatch, gc_home=tmp_path)
    paths = gc_paths()
    assert paths["state"] == tmp_path
    assert paths["config"] == tmp_path
    assert paths["cache"] == tmp_path / "cache"
    assert paths["data"] == tmp_path


def test_gc_paths_pointed_state_dir_overrides_gc_home(clear_gc_env, monkeypatch, tmp_path):
    monkeypatch.setattr("sys.platform", "linux")
    custom_state = tmp_path / "state"
    _isolate_env(monkeypatch, gc_home=tmp_path / "home", gc_state_dir=custom_state)
    paths = gc_paths()
    assert paths["state"] == custom_state
    # config/cache/data — от GC_HOME
    assert paths["config"] == tmp_path / "home"
    assert paths["cache"] == tmp_path / "home" / "cache"


from gc_core.config_modules.env_vars import effective_config_dir


def test_effective_config_dir_default_when_usable(clear_gc_env, monkeypatch, tmp_path):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _isolate_env(monkeypatch)
    # ~/.config писабельна (tmp) → config остаётся под XDG
    assert effective_config_dir() == tmp_path / ".config" / "gc"


def test_effective_config_dir_falls_back_to_dot_gc_when_config_locked(clear_gc_env, monkeypatch, tmp_path):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _isolate_env(monkeypatch)
    # имитируем залоченный ~/.config: каталог нельзя создать
    monkeypatch.setattr(env_vars_module, "_config_dir_usable", lambda p: False)
    assert effective_config_dir() == tmp_path / ".gc"


def test_effective_config_dir_respects_gc_home_no_fallback(clear_gc_env, monkeypatch, tmp_path):
    monkeypatch.setattr("sys.platform", "linux")
    _isolate_env(monkeypatch, gc_home=tmp_path / "home")
    # явный GC_HOME — фоллбэка нет, даже если каталог «нельзя создать»
    monkeypatch.setattr(env_vars_module, "_config_dir_usable", lambda p: False)
    assert effective_config_dir() == tmp_path / "home"
