import pytest

from gc_testkit.tui_support import tui_settings


def test_level_override_survives_reload_config(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_tui.app import TuiApp
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text("level: max\nagent:\n  model: m\n", encoding="utf-8")
    app = TuiApp(threads_dir=str(tmp_path / "t"), settings=tui_settings({"agent.model": "m", "level": "auto"}), level_override="auto")
    app.set_config_path(str(cfg_path))
    app.reload_config()
    assert app.effective_settings().settings.level == "auto"       # флаг пережил перечитывание


def test_level_override_survives_profile_remerge(tmp_path):
    """Пере-мерж сессионного профиля (`_profile_remerge`) — второй, независимый от
    `reload_config` сайт наложения `--level`; оба обязаны сохранять флаг запуска."""
    pytest.importorskip("textual")
    import yaml
    from gc_core.config import load_settings
    from gc_tui.app import TuiApp

    base = tmp_path / "config.yml"
    base.write_text(yaml.safe_dump({
        "backend": {"api_mode": "chat_completions",
                    "completions_url": "http://base/v1/chat/completions"},
    }), encoding="utf-8")
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "work.yml").write_text(yaml.safe_dump({
        "models": {"flash": {"url": "http://work/v1/chat/completions", "token": ""}},
        "slots": {"default": "flash"},
    }), encoding="utf-8")

    loaded = load_settings(str(base))
    app = TuiApp(threads_dir=str(tmp_path / "t"), settings=loaded, level_override="auto")
    app.set_config_path(str(base))

    app.set_session_override("profile", "work")   # триггерит _profile_remerge

    assert "flash" in app.effective_settings().settings.models     # пере-мерж реально сработал
    assert app.effective_settings().settings.level == "auto"        # флаг пережил пере-мерж профиля


def test_gc_ua_parser_accepts_level_auto():
    # Разбор аргументов вынесен в отдельную функцию, чтобы тест мог дёрнуть
    # парсер без запуска TUI.
    from gc_tui import _parse_args
    args = _parse_args(["--level", "auto"])
    assert args.level == "auto"
