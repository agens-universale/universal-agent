import pytest

from gc_testkit.tui_support import tui_settings


def test_model_override_survives_reload_config(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_tui.app import TuiApp

    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text("model: max\nagent:\n  model: m\n", encoding="utf-8")
    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m", "model": "auto"}),
        model_override="auto",
    )
    app.set_config_path(str(cfg_path))
    app.reload_config()
    assert (
        app.effective_settings().settings.model == "auto"
    )  # флаг пережил перечитывание


def test_model_override_survives_profile_remerge(tmp_path):
    """Пере-мерж сессионного профиля (`_profile_remerge`) — второй, независимый от
    `reload_config` сайт наложения `--model`; оба обязаны сохранять флаг запуска."""
    pytest.importorskip("textual")
    import yaml
    from gc_core.config import load_settings
    from gc_tui.app import TuiApp

    base = tmp_path / "config.yml"
    base.write_text(
        yaml.safe_dump(
            {
                "backend": {
                    "api_mode": "chat_completions",
                    "completions_url": "http://base/v1/chat/completions",
                },
            }
        ),
        encoding="utf-8",
    )
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "work.yml").write_text(
        yaml.safe_dump(
            {
                "models": {
                    "flash": {
                        "url": "http://work/v1/chat/completions",
                        "token": "",
                    }
                },
                "slots": {"default": "flash"},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_settings(str(base))
    app = TuiApp(
        threads_dir=str(tmp_path / "t"), settings=loaded, model_override="auto"
    )
    app.set_config_path(str(base))

    app.set_session_override("profile", "work")  # триггерит _profile_remerge

    assert (
        "flash" in app.effective_settings().settings.models
    )  # пере-мерж реально сработал
    assert (
        app.effective_settings().settings.model == "auto"
    )  # флаг пережил пере-мерж профиля


def _loaded_with_registry():
    return tui_settings(
        {
            "models": {"flash": {"model": "m"}, "pro": {"model": "p"}},
            "slots": {"default": "flash"},
        }
    )


def _applied(value):
    from gc_core.config import apply_cli_overrides
    from gc_tui import _parse_args

    args = _parse_args(["--model", value])
    return apply_cli_overrides(
        _loaded_with_registry(), extra_overrides={"model": args.model}
    )


def test_model_flag_auto_is_a_choice():
    assert _applied("auto").settings.model == "auto"


def test_model_flag_alias_is_a_choice():
    assert _applied("pro").settings.model == "pro"


def test_model_flag_level_word_is_a_choice():
    assert _applied("max").settings.model == "max"


def test_model_flag_raw_id_forces_legacy():
    from gc_core.config import cli_forces_legacy_model

    loaded = _applied("raw-id")
    assert loaded.settings.agent.model == "raw-id"
    assert cli_forces_legacy_model(loaded)


def test_level_flag_errors_with_model_hint(capsys):
    from gc_tui import _parse_args

    with pytest.raises(SystemExit) as exc:
        _parse_args(["--level", "max"])
    assert exc.value.code == 2
    assert "--model" in capsys.readouterr().err


def test_model_flag_choice_stays_a_choice_under_profile_without_alias(
    tmp_path,
):
    """`--model <алиас>` решён выбором один раз на старте: профиль без этого
    алиаса не превращает флаг в легаси-форс — выбор санируется (§3.2)."""
    pytest.importorskip("textual")
    import yaml
    from gc_core.config import (
        apply_cli_overrides,
        cli_forces_legacy_model,
        load_settings,
    )
    from gc_tui.app import TuiApp

    pair = {"url": "http://x/v1/chat/completions", "token": ""}
    base = tmp_path / "config.yml"
    base.write_text(
        yaml.safe_dump(
            {"backend": {"api_mode": "chat_completions"}, "profile": "work"}
        ),
        encoding="utf-8",
    )
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "work.yml").write_text(
        yaml.safe_dump(
            {"models": {"flash": pair}, "slots": {"default": "flash"}}
        ),
        encoding="utf-8",
    )
    (profiles_dir / "other.yml").write_text(
        yaml.safe_dump({"models": {"pro": pair}, "slots": {"default": "pro"}}),
        encoding="utf-8",
    )
    loaded = apply_cli_overrides(
        load_settings(str(base)), extra_overrides={"model": "flash"}
    )
    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=loaded,
        model_override="flash",
    )
    app.set_config_path(str(base))
    assert app.effective_settings().settings.model == "flash"

    app.set_session_override("profile", "other")

    effective = app.effective_settings()
    assert not cli_forces_legacy_model(effective)
    assert effective.settings.model == "flash"
    assert app.display_router().resolve("main").alias == "pro"
