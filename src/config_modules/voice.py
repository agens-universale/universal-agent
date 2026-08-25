from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class VoiceConfig(BaseModel):
    """Секция voice: — голосовой ввод TUI (STT). Пустой url = фича выключена."""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # Провайдер один — /v1/audio/transcriptions-совместимый сервис (локальная батарейка
    # или облако). Поле оставлено точкой расширения: появится второй протокол — добавим
    # значение, не ломая конфиги, где оно уже прописано.
    provider: Literal["openai"] = "openai"
    url: str = ""              # полный endpoint /v1/audio/transcriptions
    token: str = ""            # Bearer; локальному сервису и открытым endpoint'ам не нужен
    model: str = "whisper-1"
    language: str = "ru"
    max_seconds: int = 120     # предохранитель: авто-стоп записи
    timeout_seconds: int = 60
    verify_ssl: bool | str = Field(default=True, alias="verify-ssl")


def voice_section_filled(voice: dict) -> bool:
    """Юзер начал настраивать voice — непусты url или token."""
    return bool(voice.get("url") or voice.get("token"))


def voice_enabled(voice: dict) -> bool:
    """Конфига достаточно для работы: нужен url.

    Токен опционален: локальный сервис и открытые STT-эндпоинты авторизации не
    требуют (транскрайбер шлёт пустой Bearer — сервис его игнорирует). Требовать
    его значило бы гасить Ctrl+T на рабочей конфигурации."""
    return bool(voice.get("url"))
