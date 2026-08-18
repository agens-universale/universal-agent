"""Проводка онбординга в TUI: --init-config, режим review, стоп-гайд.

Жил в корневом tests/test_onboarding_wiring.py вместе с проверками CLI. Файл
выглядел кросс-пакетным по импортам, но взаимодействия cli ↔ tui в нём не было:
каждый фронтенд общается с ядром независимо. Разнесён по пакетам 18.08.2026.
"""
import sys

from gc_core import config
from gc_core.config_modules import env_vars
import gc_tui as tui


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(env_vars, "gc_paths", lambda: {
        "config": tmp_path / "gc", "state": tmp_path / "s",
        "data": tmp_path / "d", "cache": tmp_path / "c",
    })
    monkeypatch.setattr(env_vars, "project_slug", lambda: "p")
    monkeypatch.setattr(config, "_project_local_config_path", lambda: None)


def test_tui_init_config_creates_and_exits(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    rc = tui.main(["--init-config"])
    assert rc == 0
    assert (tmp_path / "gc" / "config.yml").is_file()
    assert "config.yml" in capsys.readouterr().err


def test_tui_init_config_runs_review_and_exits(tmp_path, monkeypatch):
    # --init-config + interactive → app._onboarding_mode == "review"; app.run() вызван
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(tui, "_interactive", lambda: True)

    from gc_core import thread_store as ts_mod
    monkeypatch.setattr(ts_mod, "resolve_threads_dir", lambda cfg: str(tmp_path / "threads"))

    from gc_core import mcp_daemon_autostart as mcp_da_mod
    monkeypatch.setattr(mcp_da_mod, "ensure_mcp_daemons", lambda cfg: None, raising=False)
    monkeypatch.setattr(mcp_da_mod, "shutdown_mcp_daemons", lambda: None, raising=False)

    captured = {}

    import sys
    from gc_tui import app as app_mod
    OrigTuiApp = app_mod.TuiApp

    class FakeTuiApp(OrigTuiApp):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            captured["app"] = self

        def run(self, **kwargs):
            captured["ran"] = True

    monkeypatch.setattr(app_mod, "TuiApp", FakeTuiApp)
    monkeypatch.setitem(sys.modules, "gc_tui.app", app_mod)

    rc = tui.main(["--init-config"])
    assert rc == 0
    assert captured.get("ran") is True, "app.run() должен быть вызван"
    assert captured["app"]._onboarding_mode == "review"


def test_tui_stop_guide_on_unconfigured(tmp_path, monkeypatch, capsys):
    # дефолтный путь (без --config), свежий конфиг с плейсхолдером →
    # стоп-гайд и выход 0, TUI не запускаем; болванка всё равно создаётся.
    _isolate(tmp_path, monkeypatch)
    rc = tui.main([])
    assert rc == 0
    assert "gc-ua --init-config" in capsys.readouterr().err   # стоп-гайд с помощником, program=gc-ua
    assert (tmp_path / "gc" / "config.yml").is_file()


def test_tui_proceeds_when_configured(tmp_path, monkeypatch):
    # настроенный конфиг → стоп-гайд НЕ срабатывает (идём дальше к TUI).
    _isolate(tmp_path, monkeypatch)
    cfgdir = tmp_path / "gc"
    cfgdir.mkdir(parents=True)
    (cfgdir / "config.yml").write_text(
        "backend:\n"
        "  completions_url: https://real/v1/chat/completions\n"
        "  chat_completion_headers:\n"
        "    Authorization: Bearer real-token\n",
        encoding="utf-8",
    )
    cfg = config.load_config(None)
    from gc_core import onboarding
    assert onboarding.is_unconfigured(cfg) is False
