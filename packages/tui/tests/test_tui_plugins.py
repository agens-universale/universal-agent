"""TUI: свежая загрузка настроек несёт снимок набора плагинов (§8.3)."""

import pytest
from gc_core.settings import runtime_of
from gc_testkit.plugin_store import install_plugin
from gc_testkit.settings import make_settings
from gc_testkit.tui_support import tui_settings

pytest.importorskip("textual")

from gc_tui import app as app_mod


def _names(loaded):
    return tuple(p.name for p in runtime_of(loaded).plugins.plugins)


def test_tui_fresh_load_has_snapshot(tmp_path, monkeypatch):
    install_plugin(tmp_path, monkeypatch)
    app = app_mod.TuiApp(settings=tui_settings({}))

    assert _names(app._with_launch_flags(make_settings({}))) == ("inst",)


def test_tui_fresh_load_respects_use_installed(tmp_path, monkeypatch):
    install_plugin(tmp_path, monkeypatch)
    app = app_mod.TuiApp(settings=tui_settings({}))
    loaded = make_settings({"plugins.use_installed": False})

    assert _names(app._with_launch_flags(loaded)) == ()


def test_tui_startup_settings_have_snapshot(tmp_path, monkeypatch):
    install_plugin(tmp_path, monkeypatch)
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        "backend:\n  completions_url: http://127.0.0.1:9\n"
        f'paths:\n  threads_data_path: "{(tmp_path / "t").as_posix()}"\n',
        encoding="utf-8",
    )
    started = {}

    class _FakeApp:
        def __init__(self, **kw):
            started.update(kw)

        def set_config_path(self, path):
            self._config_path = path

        def _maybe_notice_profile_activation(self, *_a, **_k):
            pass

        def run(self):
            return None

    monkeypatch.setattr(app_mod, "TuiApp", _FakeApp)
    monkeypatch.setattr(
        "recommended_mcp.runtime.daemon_autostart.shutdown_mcp_daemons",
        lambda: None,
    )
    from gc_tui import main

    assert main(argv=["--config", str(cfg_path), "--no-auto-mcp"]) == 0
    assert _names(started["settings"]) == ("inst",)
