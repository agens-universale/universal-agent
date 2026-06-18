"""Онбординг первого запуска: детект ненастроенного конфига + текст-гайд."""
import yaml

import config
import onboarding
from bundled_assets import bundled_root
from config_modules import env_vars


def _default_yaml() -> dict:
    return yaml.safe_load(
        (bundled_root() / "config.default.yml").read_text(encoding="utf-8")
    )


def _isolated_default(tmp_path, monkeypatch) -> dict:
    """Загрузить config.default.yml реальным загрузчиком, изолировав от
    машинного ~/.gc/config.local.yml и project-local <cwd>/.gc/config.yml."""
    monkeypatch.setattr(env_vars, "gc_paths", lambda: {
        "config": tmp_path / "gc", "state": tmp_path / "s",
        "data": tmp_path / "d", "cache": tmp_path / "c",
    })
    monkeypatch.setattr(config, "_project_local_config_path", lambda: None)
    return config.load_config(str(bundled_root() / "config.default.yml"))


def test_default_config_is_unconfigured(tmp_path, monkeypatch):
    # реальная (плоская) форма из загрузчика → дефолт не настроен
    cfg = _isolated_default(tmp_path, monkeypatch)
    assert onboarding.is_unconfigured(cfg) is True


def test_configured_when_url_and_token_set():
    cfg = {
        "completions_url": "https://real/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer real-token"},
    }
    assert onboarding.is_unconfigured(cfg) is False


def test_unconfigured_if_token_still_placeholder():
    cfg = {
        "completions_url": "https://real/v1/chat/completions",
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
    }
    assert onboarding.is_unconfigured(cfg) is True


def test_sentinels_match_default_yaml():
    # страж: шаблон config.default.yml и сентинелы детекта не должны разъезжаться
    raw = _default_yaml()
    assert raw["backend"]["completions_url"] == onboarding.URL_SENTINEL
    assert (
        raw["backend"]["chat_completion_headers"]["Authorization"]
        == onboarding.TOKEN_SENTINEL
    )


def test_setup_guide_lists_pending_fields_and_path(tmp_path, monkeypatch):
    cfg = _isolated_default(tmp_path, monkeypatch)
    text = onboarding.setup_guide("/home/u/.gc/config.yml", cfg)
    assert "/home/u/.gc/config.yml" in text
    assert "backend.completions_url" in text
    assert "Authorization" in text
