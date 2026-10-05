"""launch_root snapshot на старте TUI — cwd на момент входа, не live Path.cwd()."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import gc_core.config as _cfg_mod
import gc_core.config_modules.env_vars as _env_mod
import gc_core.onboarding as _onb_mod
import gc_core.thread_store as _ts_mod
import gc_tui as tui_pkg
from gc_core.config_modules.profiles import ProfileMeta
from gc_testkit.settings import make_settings


def _loaded(tmp_path):
    """Загруженные настройки старта: каталог тредов и пустой профиль."""
    return replace(
        make_settings({"paths.threads_data_path": str(tmp_path / "threads")}),
        profile_meta=ProfileMeta(active_name=None, ref=None, mtime_set={}),
    )


def test_tui_main_snapshots_launch_root(tmp_path, monkeypatch):
    """Перед созданием TuiApp в настройках (runtime) фиксируется launch_root = cwd.resolve()."""
    monkeypatch.chdir(tmp_path)

    captured: dict = {}

    class FakeApp:
        def __init__(self, *, threads_dir, system, settings, **kw):
            captured["settings"] = settings
            self._config_path = None
            self._config_project_local = None

        def _maybe_notice_profile_activation(self, *a, **kw):
            # Точка входа develop зовёт её на app перед run(); заглушке достаточно
            # не падать — тест снимает launch_root, а не поведение профилей.
            pass

        def set_config_path(
            self, path
        ):  # как у настоящего TuiApp: путь + снимок mtime
            self._config_path = path

        def run(self):
            return None

    fake_mod = MagicMock()
    fake_mod.TuiApp = FakeApp

    monkeypatch.setattr(
        tui_pkg.importlib,
        "import_module",
        lambda name, *a: fake_mod if name == "gc_tui.app" else MagicMock(),
    )
    # Подмена ПО ОБЪЕКТАМ модулей, а не по строкам: строковую форму monkeypatch
    # резолвит через `importlib.import_module`, а он здесь подменён на заглушку —
    # строковые адреса попадали бы в MagicMock и не делали ничего (тест зеленел,
    # ничего не проверяя). Порядок важен: реальные модули берём ДО подмены
    # importlib.
    monkeypatch.setattr(
        _cfg_mod, "load_settings", lambda *a, **k: _loaded(tmp_path)
    )
    monkeypatch.setattr(
        _cfg_mod, "apply_cli_overrides", lambda loaded, **_kw: loaded
    )
    monkeypatch.setattr(
        _cfg_mod, "resolve_config_path", lambda _p: str(tmp_path / "c.yml")
    )
    monkeypatch.setattr(_onb_mod, "is_unconfigured", lambda _cfg: False)
    monkeypatch.setattr(
        _ts_mod, "resolve_threads_dir", lambda _cfg: str(tmp_path / "threads")
    )
    monkeypatch.setattr(_env_mod, "ensure_project_gc_dir", lambda: None)
    monkeypatch.setattr(_env_mod, "project_local_config_path", lambda: None)

    rc = tui_pkg.main([])
    assert rc == 0
    assert captured["settings"].runtime.launch_root == str(tmp_path.resolve())


def test_tui_main_launch_root_not_live_cwd(tmp_path, monkeypatch):
    """После snapshot смена cwd не должна менять уже зафиксированный launch_root."""
    start = tmp_path / "start"
    other = tmp_path / "other"
    start.mkdir()
    other.mkdir()
    monkeypatch.chdir(start)

    captured: dict = {}

    class FakeApp:
        def __init__(self, *, threads_dir, system, settings, **kw):
            # Имитация: cwd уже сменился к моменту конструктора.
            import os

            os.chdir(other)
            captured["settings"] = settings
            self._config_path = None
            self._config_project_local = None

        def _maybe_notice_profile_activation(self, *a, **kw):
            # Точка входа develop зовёт её на app перед run(); заглушке достаточно
            # не падать — тест снимает launch_root, а не поведение профилей.
            pass

        def set_config_path(
            self, path
        ):  # как у настоящего TuiApp: путь + снимок mtime
            self._config_path = path

        def run(self):
            return None

    fake_mod = MagicMock()
    fake_mod.TuiApp = FakeApp

    monkeypatch.setattr(
        tui_pkg.importlib,
        "import_module",
        lambda name, *a: fake_mod if name == "gc_tui.app" else MagicMock(),
    )
    # Подмена ПО ОБЪЕКТАМ модулей, а не по строкам: строковую форму monkeypatch
    # резолвит через `importlib.import_module`, а он здесь подменён на заглушку —
    # строковые адреса попадали бы в MagicMock и не делали ничего (тест зеленел,
    # ничего не проверяя). Порядок важен: реальные модули берём ДО подмены
    # importlib.
    monkeypatch.setattr(
        _cfg_mod, "load_settings", lambda *a, **k: _loaded(tmp_path)
    )
    monkeypatch.setattr(
        _cfg_mod, "apply_cli_overrides", lambda loaded, **_kw: loaded
    )
    monkeypatch.setattr(
        _cfg_mod, "resolve_config_path", lambda _p: str(tmp_path / "c.yml")
    )
    monkeypatch.setattr(_onb_mod, "is_unconfigured", lambda _cfg: False)
    monkeypatch.setattr(
        _ts_mod, "resolve_threads_dir", lambda _cfg: str(tmp_path / "threads")
    )
    monkeypatch.setattr(_env_mod, "ensure_project_gc_dir", lambda: None)
    monkeypatch.setattr(_env_mod, "project_local_config_path", lambda: None)

    assert tui_pkg.main([]) == 0
    assert captured["settings"].runtime.launch_root == str(start.resolve())
    assert Path.cwd().resolve() == other.resolve()
