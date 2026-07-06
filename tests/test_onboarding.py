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
    """config.default.yml реальным загрузчиком, изолировав машинный и project-local overlay."""
    monkeypatch.setattr(env_vars, "gc_paths", lambda: {
        "config": tmp_path / "gc", "state": tmp_path / "s",
        "data": tmp_path / "d", "cache": tmp_path / "c",
    })
    monkeypatch.setattr(config, "_project_local_config_path", lambda: None)
    return config.load_config(str(bundled_root() / "config.default.yml"))


# --- базовый детект -------------------------------------------------------

def test_default_config_is_unconfigured(tmp_path, monkeypatch):
    cfg = _isolated_default(tmp_path, monkeypatch)
    assert onboarding.is_unconfigured(cfg) is True


def test_configured_when_url_token_model_set():
    cfg = {
        "completions_url": "https://real/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer real-token"},
        "model": "gigacode_agent_v1.6_rc",
    }
    assert onboarding.is_unconfigured(cfg) is False


def test_unconfigured_if_token_still_placeholder():
    cfg = {
        "completions_url": "https://real/v1/chat/completions",
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
        "model": "gigacode_agent_v1.6_rc",
    }
    assert onboarding.is_unconfigured(cfg) is True


# --- auth: токен ИЛИ agent_auth ------------------------------------------

def test_agent_auth_satisfies_auth_without_token():
    cfg = {
        "completions_url": "https://real/v1/chat/completions",
        "model": "gigacode_agent_v1.6_rc",
        "agent_auth": {"auth_endpoint": "https://auth.local/token"},
    }
    assert onboarding.is_unconfigured(cfg) is False


def test_agent_auth_set_drops_token_from_guide():
    cfg = {
        "completions_url": "https://real/v1/chat/completions",
        "model": "gigacode_agent_v1.6_rc",
        "agent_auth": {"auth_endpoint": "https://auth.local/token"},
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
    }
    text = onboarding.setup_guide("/home/u/.gc/config.yml", cfg)
    assert "Authorization" not in text
    assert "настроен" in text


def test_no_token_and_no_complete_agent_auth_is_configured():
    """Нет токен-заголовка и нет полноценного agent_auth, но и нет шаблонного
    плейсхолдера → конфиг настроен. Локальный прокси / эндпоинт без auth сам ставит
    авторизацию; гейт ловит только незаполненный сентинел, а не намеренное отсутствие.
    `agent_auth: {}` — то, во что config.py:277 разворачивает отсутствие секции."""
    cfg = {
        "completions_url": "http://127.0.0.1:8014/v1/chat/completions",
        "model": "MiniMaxAI/MiniMax-M2.7",
        "agent_auth": {},
    }
    assert onboarding.is_unconfigured(cfg) is False


# --- adaptive model ------------------------------------------------------

def test_model_sentinel_is_pending():
    cfg = {
        "completions_url": "https://real/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer real-token"},
        "model": onboarding.MODEL_SENTINEL,
    }
    text = onboarding.setup_guide("/p", cfg)
    assert "agent.model" in text


def test_filled_model_not_in_guide():
    cfg = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
        "model": "gigacode_agent_v1.6_rc",
    }
    text = onboarding.setup_guide("/p", cfg)
    assert "имя модели" not in text   # модель не в списке полей к заполнению


# --- ремарка про preset-модель -------------------------------------------

def test_setup_guide_notes_preset_model_when_pending():
    cfg = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
        "model": "gigacode_agent_v1.6_rc",
    }
    text = onboarding.setup_guide("/p", cfg)
    assert "Модель по умолчанию уже задана" in text
    assert "gigacode_agent_v1.6_rc" in text


def test_setup_guide_no_preset_model_note_when_model_pending():
    cfg = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
        "model": onboarding.MODEL_SENTINEL,
    }
    text = onboarding.setup_guide("/p", cfg)
    assert "Модель по умолчанию уже задана" not in text
    assert "имя модели" in text   # модель в списке pending


# --- позитивное сообщение, когда всё покрыто -----------------------------

def test_setup_guide_positive_when_all_covered():
    cfg = {
        "completions_url": "https://real/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer real-token"},
        "model": "gigacode_agent_v1.6_rc",
    }
    text = onboarding.setup_guide("/home/u/.gc/config.yml", cfg)
    assert "настроен" in text
    assert "можно запускать" in text
    assert "•" not in text


# --- страж сентинелов vs config.default.yml ------------------------------

def test_sentinels_match_default_yaml():
    raw = _default_yaml()
    assert raw["backend"]["completions_url"] == onboarding.URL_SENTINEL
    assert (
        raw["backend"]["chat_completion_headers"]["Authorization"]
        == onboarding.TOKEN_SENTINEL
    )
    assert raw["agent"]["model"]
    assert raw["agent"]["model"] != onboarding.MODEL_SENTINEL


def test_setup_guide_lists_pending_fields_and_path(tmp_path, monkeypatch):
    cfg = _isolated_default(tmp_path, monkeypatch)
    text = onboarding.setup_guide("/home/u/.gc/config.yml", cfg)
    assert "/home/u/.gc/config.yml" in text
    assert "backend.completions_url" in text
    assert "Authorization" in text
    assert "имя модели" not in text   # модель задана в дефолте → не в списке полей
