"""Секция voice: — дефолты, alias verify-ssl, wiring в AppConfig, enabled-критерий."""

import pytest
from gc_core.config_modules.voice import (
    VoiceConfig,
    voice_enabled,
    voice_section_filled,
)


def test_voice_config_defaults():
    cfg = VoiceConfig()
    assert cfg.provider == "openai"
    assert cfg.url == ""
    assert cfg.token.get_secret_value() == ""
    assert cfg.model == "whisper-1"
    assert cfg.language == "ru"
    assert cfg.max_seconds == 120
    assert cfg.timeout_seconds == 60
    assert cfg.verify_ssl is True


def test_voice_config_verify_ssl_alias():
    cfg = VoiceConfig.model_validate({"verify-ssl": "/path/ca.pem"})
    assert cfg.verify_ssl == "/path/ca.pem"


def test_voice_in_app_config():
    from gc_core.config_modules.root import AppConfig

    app_cfg = AppConfig.model_validate(
        {"voice": {"url": "https://x", "token": "k"}}
    )
    assert app_cfg.voice.url == "https://x"
    assert app_cfg.voice.token.get_secret_value() == "k"


def test_voice_enabled_rules():
    assert voice_enabled(
        {"provider": "openai", "url": "https://x", "token": "t"}
    )
    assert not voice_enabled({"provider": "openai", "url": "", "token": "t"})


def test_voice_enabled_openai_without_token():
    """Локальный сервис и открытые STT-эндпоинты авторизации не требуют — токен
    для openai опционален (транскрайбер шлёт пустой Bearer, сервис его игнорирует).
    Раньше гейт требовал token и гасил Ctrl+T на рабочей конфигурации."""
    assert voice_enabled(
        {
            "provider": "openai",
            "url": "http://127.0.0.1:8735/v1/audio/transcriptions",
            "token": "",
        }
    )


def test_voice_section_filled():
    assert not voice_section_filled({"url": "", "token": ""})
    assert voice_section_filled({"url": "https://x", "token": ""})
    assert voice_section_filled({"url": "", "token": "k"})
    assert not voice_section_filled(
        {"provider": "openai", "url": "", "token": ""}
    )


def test_voice_overridden_fact_computed(tmp_path, monkeypatch):
    """load_config кладёт voice_overridden_by_managed при живом managed-перекрытии."""
    from gc_core import config as config_mod

    base = tmp_path / "config.yml"
    base.write_text(
        "voice:\n  url: https://api.groq.com/openai/v1/audio/transcriptions\n  token: gsk\n",
        encoding="utf-8",
    )
    managed = tmp_path / "config.local.yml"
    managed.write_text(
        "voice:\n  url: http://127.0.0.1:8735/v1/audio/transcriptions\n  token: local\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "gc_core.config_modules.env_vars.managed_config_path", lambda: managed
    )
    cfg = config_mod.load_config(str(base))
    assert (
        cfg["voice_overridden_by_managed"]
        == "https://api.groq.com/openai/v1/audio/transcriptions"
    )
    assert cfg["voice"]["url"].endswith(":8735/v1/audio/transcriptions")


def test_voice_override_notice_is_silent_when_the_project_layer_wins(
    tmp_path, monkeypatch
):
    """Project-слой поверх managed: адрес батарейки уже не действует — и notice молчит.

    Находка ревью коллеги: факт перекрытия считался МЕЖДУ managed и project-слоями,
    поэтому агент на старте предупреждал «managed перекрыл ваш voice», хотя реально
    работал адрес из <cwd>/.gc/config.yml, а managed не решал ничего."""
    from gc_core import config as config_mod

    base = tmp_path / "config.yml"
    base.write_text(
        "voice:\n  url: https://api.groq.com/openai/v1/audio/transcriptions\n  token: gsk\n",
        encoding="utf-8",
    )
    managed = tmp_path / "config.local.yml"
    managed.write_text(
        "voice:\n  url: http://127.0.0.1:8735/v1/audio/transcriptions\n  token: local\n",
        encoding="utf-8",
    )
    project = tmp_path / "project.yml"
    project.write_text(
        "voice:\n  url: https://project.example/v1/audio/transcriptions\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "gc_core.config_modules.env_vars.managed_config_path", lambda: managed
    )
    monkeypatch.setattr(
        "gc_core.config_modules.env_vars.project_local_config_path",
        lambda: str(project),
    )

    cfg = config_mod.load_config(str(base))
    assert (
        cfg["voice"]["url"]
        == "https://project.example/v1/audio/transcriptions"
    )
    assert cfg["voice_overridden_by_managed"] is None


def test_voice_override_notice_survives_a_project_layer_about_other_keys(
    tmp_path, monkeypatch
):
    """Project трогает не адрес, а язык — адрес батарейки действует, и notice остаётся."""
    from gc_core import config as config_mod

    base = tmp_path / "config.yml"
    base.write_text(
        "voice:\n  url: https://api.groq.com/openai/v1/audio/transcriptions\n  token: gsk\n",
        encoding="utf-8",
    )
    managed = tmp_path / "config.local.yml"
    managed.write_text(
        "voice:\n  url: http://127.0.0.1:8735/v1/audio/transcriptions\n  token: local\n",
        encoding="utf-8",
    )
    project = tmp_path / "project.yml"
    project.write_text("voice:\n  language: en\n", encoding="utf-8")
    monkeypatch.setattr(
        "gc_core.config_modules.env_vars.managed_config_path", lambda: managed
    )
    monkeypatch.setattr(
        "gc_core.config_modules.env_vars.project_local_config_path",
        lambda: str(project),
    )

    cfg = config_mod.load_config(str(base))
    assert cfg["voice"]["language"] == "en"
    assert (
        cfg["voice_overridden_by_managed"]
        == "https://api.groq.com/openai/v1/audio/transcriptions"
    )


def test_voice_overridden_fact_ignores_the_address_form(tmp_path, monkeypatch):
    """Человек вписал адрес батарейки как localhost, батарейка пишет 127.0.0.1: сервис
    один, перекрывать нечего. Установка это уже понимает (user_voice_conflict) — старт
    обязан отвечать так же, иначе один факт получает два разных вердикта."""
    from gc_core import config as config_mod

    base = tmp_path / "config.yml"
    base.write_text(
        "voice:\n  url: http://localhost:8735/v1/audio/transcriptions\n",
        encoding="utf-8",
    )
    managed = tmp_path / "config.local.yml"
    managed.write_text(
        "voice:\n  url: http://127.0.0.1:8735/v1/audio/transcriptions\n  model: small\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "gc_core.config_modules.env_vars.managed_config_path", lambda: managed
    )
    cfg = config_mod.load_config(str(base))
    assert cfg["voice_overridden_by_managed"] is None


def test_voice_overridden_fact_none_without_managed(tmp_path):
    from gc_core import config as config_mod

    base = tmp_path / "config.yml"
    base.write_text("voice:\n  url: https://x\n  token: t\n", encoding="utf-8")
    cfg = config_mod.load_config(str(base))
    assert cfg["voice_overridden_by_managed"] is None


def test_voice_overridden_fact_token_only(tmp_path, monkeypatch):
    """База только с token (без url) — перекрытие фиксируется как "<без url>"."""
    from gc_core import config as config_mod

    base = tmp_path / "config.yml"
    base.write_text("voice:\n  token: gsk\n", encoding="utf-8")
    managed = tmp_path / "config.local.yml"
    managed.write_text(
        "voice:\n  url: http://127.0.0.1:8735/v1/audio/transcriptions\n  token: local\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "gc_core.config_modules.env_vars.managed_config_path", lambda: managed
    )
    cfg = config_mod.load_config(str(base))
    assert cfg["voice_overridden_by_managed"] == "<без url>"


def test_broken_voice_section_fails_validation_not_arithmetic(tmp_path):
    """`voice: строка` — ошибка СХЕМЫ с именем ключа, а не техническая из `dict()`.

    Находка ревью коллеги: снимок базовой секции снимался ДО валидации
    (`dict(raw_config.get("voice"))`), и на не-словаре человек получал
    «dictionary update sequence element #0 has length 1» — про собственный конфиг он
    из такого сообщения не узнавал ничего."""
    from gc_core import config as config_mod

    base = tmp_path / "config.yml"
    base.write_text("voice: broken\n", encoding="utf-8")

    # отказ схемы приходит завёрнутым (MR #80, L28) — ключ назван, дампа нет
    with pytest.raises(ValueError, match="не проходит схему") as exc:
        config_mod.load_config(str(base))
    assert "voice" in str(exc.value)
