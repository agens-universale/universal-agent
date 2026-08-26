"""launch_root snapshot на старте TUI — cwd на момент входа, не live Path.cwd()."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import gc_tui as tui_pkg


def test_tui_main_snapshots_launch_root(tmp_path, monkeypatch):
    """Перед созданием TuiApp в config_dict фиксируется launch_root = cwd.resolve()."""
    monkeypatch.chdir(tmp_path)

    captured: dict = {}

    class FakeApp:
        def __init__(self, *, threads_dir, system, config):
            captured["config"] = config
            self._config_path = None
            self._config_project_local = None

        def set_config_path(self, path):   # как у настоящего TuiApp: путь + снимок mtime
            self._config_path = path

        def _maybe_notice_profile_activation(self, *args, **kwargs):   # заглушка §3.5-нотиса
            return None

        def run(self):
            return None

    fake_mod = MagicMock()
    fake_mod.TuiApp = FakeApp

    from gc_core.config_modules.profiles import ProfileMeta

    monkeypatch.setattr(
        tui_pkg.importlib, "import_module", lambda name, package=None: fake_mod if name == "gc_tui.app" else MagicMock()
    )
    monkeypatch.setattr(
        "gc_core.config.load_config_ex",
        lambda _path=None, **_kw: (
            {"threads_data_path": str(tmp_path / "threads")},
            ProfileMeta(active_name=None, ref=None, mtime_set={}),
        ),
    )
    monkeypatch.setattr("gc_core.config.apply_cli_overrides", lambda _cfg: None)
    monkeypatch.setattr("gc_core.onboarding.is_unconfigured", lambda _cfg: False)
    monkeypatch.setattr(
        "gc_core.thread_store.resolve_threads_dir",
        lambda _cfg: str(tmp_path / "threads"),
    )
    monkeypatch.setattr("gc_core.config.resolve_config_path", lambda _p: str(tmp_path / "c.yml"))
    monkeypatch.setattr("gc_core.config._project_local_config_path", lambda: None)
    monkeypatch.setattr(
        "gc_core.config_modules.env_vars.ensure_project_gc_dir",
        lambda: None,
    )

    rc = tui_pkg.main([])
    assert rc == 0
    assert captured["config"]["launch_root"] == str(tmp_path.resolve())


def test_tui_main_launch_root_not_live_cwd(tmp_path, monkeypatch):
    """После snapshot смена cwd не должна менять уже зафиксированный launch_root."""
    start = tmp_path / "start"
    other = tmp_path / "other"
    start.mkdir()
    other.mkdir()
    monkeypatch.chdir(start)

    captured: dict = {}

    class FakeApp:
        def __init__(self, *, threads_dir, system, config):
            # Имитация: cwd уже сменился к моменту конструктора.
            import os

            os.chdir(other)
            captured["config"] = config
            self._config_path = None
            self._config_project_local = None

        def set_config_path(self, path):   # как у настоящего TuiApp: путь + снимок mtime
            self._config_path = path

        def _maybe_notice_profile_activation(self, *args, **kwargs):   # заглушка §3.5-нотиса
            return None

        def run(self):
            return None

    fake_mod = MagicMock()
    fake_mod.TuiApp = FakeApp

    from gc_core.config_modules.profiles import ProfileMeta

    monkeypatch.setattr(
        tui_pkg.importlib, "import_module", lambda name, package=None: fake_mod if name == "gc_tui.app" else MagicMock()
    )
    monkeypatch.setattr(
        "gc_core.config.load_config_ex",
        lambda _path=None, **_kw: (
            {"threads_data_path": str(tmp_path / "threads")},
            ProfileMeta(active_name=None, ref=None, mtime_set={}),
        ),
    )
    monkeypatch.setattr("gc_core.config.apply_cli_overrides", lambda _cfg: None)
    monkeypatch.setattr("gc_core.onboarding.is_unconfigured", lambda _cfg: False)
    monkeypatch.setattr(
        "gc_core.thread_store.resolve_threads_dir",
        lambda _cfg: str(tmp_path / "threads"),
    )
    monkeypatch.setattr("gc_core.config.resolve_config_path", lambda _p: str(tmp_path / "c.yml"))
    monkeypatch.setattr("gc_core.config._project_local_config_path", lambda: None)
    monkeypatch.setattr(
        "gc_core.config_modules.env_vars.ensure_project_gc_dir",
        lambda: None,
    )

    assert tui_pkg.main([]) == 0
    assert captured["config"]["launch_root"] == str(start.resolve())
    assert Path.cwd().resolve() == other.resolve()
