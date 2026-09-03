"""Wire voice-секции в machine-managed ~/.gc/config.local.yml.

config.yml пользователя не трогается; managed-слой мёржится поверх base —
install при конфликте предупреждает, startup_notices показывает постоянно."""

from __future__ import annotations

from typing import Any, Optional

from mcp_runtime.wiring import load_managed, save_managed


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


def overridden_user_voice(user_voice: Optional[dict], managed_url: str) -> Optional[str]:
    """Юзерская voice-секция, которую перекрывает managed-слой, или None.

    Единственный владелец правила на всех, кто об этом перекрытии сообщает: установка
    (сообщение в терминале) и загрузчик конфига (уведомление на старте). Два вычисления
    одного факта расходились на форме адреса — установка молчала, старт предупреждал.

    Возвращает url юзера, а если тот задал только токен — «<без url>»: секция начата,
    перекрытие для человека такое же неожиданное, и молчать о нём нельзя."""
    user = user_voice or {}
    url = str(user.get("url") or "")
    token = str(user.get("token") or "")
    if not url and not token:
        return None
    if url and _same_service(url, managed_url):
        return None                     # человек вписал адрес той же батарейки
    return url or "<без url>"


def _same_service(a: str, b: str) -> bool:
    """Адреса ведут на один сервис: тот же порт и оба — на этой машине. `localhost` и
    `127.0.0.1` человек пишет как привык, а сервис за ними один и тот же.

    Мусор вместо порта — не «тот же сервис», а НЕ он: `urlsplit(...).port` на таком
    бросает ValueError, и без этой обёртки опечатка в config.yml (`:87x5`) роняла
    ЗАПУСК АГЕНТА сырым исключением — сверка зовётся из `load_config` на каждом старте,
    как только прописана локальная батарейка. Сосед по правилу (`autostart.url_is_ours`)
    эту же ошибку ловит; здесь её пропустили."""
    from urllib.parse import urlsplit
    from voice_runtime.net import is_loopback     # ленивый: загрузчику не нужен requests
    try:
        port_a, port_b = urlsplit(a).port, urlsplit(b).port
    except ValueError:
        return False
    if port_a is None or port_a != port_b:
        return False
    return is_loopback(a) and is_loopback(b)


def base_config_raw() -> dict:
    """Сырые данные base-конфига — то, что подаётся в `user_voice_conflict`.

    Живёт рядом с правилом конфликта, а не у вызывающего: читать его нужно всем трём
    входам в установку (CLI, мастер, /settings), и когда чтение лежало только в CLI,
    оба TUI-входа подавали в проверку пустой словарь — предупреждение «перекрою ваш
    voice.url» там не показывалось никогда."""
    import yaml

    from config import resolve_config_path
    from utils.user_text import read_user_text_or_none

    # Зовётся ПЕРЕД установкой голоса из трёх входов (CLI, мастер, /settings):
    # непрочитанный конфиг значит «конфликта не видно», а не «установка упала».
    return yaml.safe_load(read_user_text_or_none(resolve_config_path(None)) or "") or {}


def user_voice_conflict(base_raw: dict, port: int) -> Optional[str]:
    """URL юзерской voice-секции из base-конфига, которую перекроет managed на `port`.

    Обёртка установки: она зовётся ДО записи managed-секции, поэтому адрес будущего
    слоя считается здесь. Само правило — в overridden_user_voice."""
    voice: Any = (base_raw or {}).get("voice") or {}
    return overridden_user_voice(voice, local_url(port))
