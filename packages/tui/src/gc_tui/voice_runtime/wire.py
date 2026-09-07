"""Wire voice-секции в machine-managed ~/.gc/config.local.yml.

config.yml пользователя не трогается; managed-слой мёржится поверх base —
install при конфликте предупреждает, startup_notices показывает постоянно."""

from __future__ import annotations

from typing import Any, Optional

from gc_core.config_modules.voice import overridden_user_voice
from recommended_mcp.runtime.wiring import load_managed, save_managed


def local_url(port: int) -> str:
    """Адрес локального сервиса — один формат на весь проект: его кладёт в конфиг wire,
    его же показывает /settings. Собирать строку по месту нельзя, разъедутся."""
    return f"http://127.0.0.1:{port}/v1/audio/transcriptions"


def voice_wire_section(port: int, model: str, language: str = "ru") -> dict:
    return {
        "provider": "openai",
        "url": local_url(port),
        "token": "local",
        "model": model,
        "language": language,
    }


def wire_voice(port: int, model: str, language: str = "ru") -> None:
    data = load_managed()
    data["voice"] = voice_wire_section(port, model, language)
    save_managed(data)


def unwire_voice() -> bool:
    data = load_managed()
    if "voice" not in data:
        return False
    del data["voice"]
    save_managed(data)
    return True


def managed_voice() -> Optional[dict]:
    return load_managed().get("voice")


def managed_language(default: str = "ru") -> str:
    """Язык уже записанной managed-секции — единственное в ней, что выбирает человек
    (/settings), а не машина. Всякий повторный wire (переустановка, возврат в локальный
    режим) переписывает секцию целиком и обязан пронести язык через себя."""
    return str((managed_voice() or {}).get("language") or default)


def base_config_raw() -> dict:
    """Сырые данные base-конфига — то, что подаётся в `user_voice_conflict`.

    Живёт рядом с правилом конфликта, а не у вызывающего: читать его нужно всем трём
    входам в установку (CLI, мастер, /settings), и когда чтение лежало только в CLI,
    оба TUI-входа подавали в проверку пустой словарь — предупреждение «перекрою ваш
    voice.url» там не показывалось никогда."""
    import yaml

    from gc_core.config import resolve_config_path
    from gc_core.utils.user_text import read_user_text_or_none

    # Зовётся ПЕРЕД установкой голоса из трёх входов (CLI, мастер, /settings):
    # непрочитанный конфиг значит «конфликта не видно», а не «установка упала».
    return yaml.safe_load(read_user_text_or_none(resolve_config_path(None)) or "") or {}


def user_voice_conflict(base_raw: dict, port: int) -> Optional[str]:
    """URL юзерской voice-секции из base-конфига, которую перекроет managed на `port`.

    Обёртка установки: она зовётся ДО записи managed-секции, поэтому адрес будущего
    слоя считается здесь. Само правило — в overridden_user_voice."""
    voice: Any = (base_raw or {}).get("voice") or {}
    return overridden_user_voice(voice, local_url(port))
