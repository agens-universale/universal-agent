from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from gc_core.utils.url_addr import same_service


class VoiceConfig(BaseModel):
    """Секция voice: — голосовой ввод TUI (STT). Пустой url = фича выключена."""

    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, frozen=True
    )

    # Провайдер один — /v1/audio/transcriptions-совместимый сервис (локальная батарейка
    # или облако). Поле оставлено точкой расширения: появится второй протокол — добавим
    # значение, не ломая конфиги, где оно уже прописано.
    provider: Literal["openai"] = "openai"
    url: str = ""  # полный endpoint /v1/audio/transcriptions
    token: str = (
        ""  # Bearer; локальному сервису и открытым endpoint'ам не нужен
    )
    model: str = "whisper-1"
    language: str = "ru"
    max_seconds: int = 120  # предохранитель: авто-стоп записи
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


def overridden_user_voice(
    user_voice: Optional[dict], managed_url: str
) -> Optional[str]:
    """Юзерская voice-секция, которую перекрывает managed-слой, или None.

    Единственный владелец правила на всех, кто об этом перекрытии сообщает: установка
    (сообщение в терминале) и загрузчик конфига (уведомление на старте). Два вычисления
    одного факта расходились на форме адреса — установка молчала, старт предупреждал.

    Возвращает url юзера, а если тот задал только токен — «<без url>»: секция начата,
    перекрытие для человека такое же неожиданное, и молчать о нём нельзя.

    Живёт здесь, а не в `voice_runtime.wire`: это семантика слоёв конфига — кто кого
    перекрыл, — а не работа с сервисом. Пока правило лежало у сервиса, `config.py` ради
    сравнения двух адресов импортировал менеджер батарейки, а тот на уровне модуля
    тянет `recommended_mcp`, которого у ядра в зависимостях нет.
    """
    user = user_voice or {}
    url = str(user.get("url") or "")
    token = str(user.get("token") or "")
    if not url and not token:
        return None
    if url and same_service(url, managed_url):
        return None  # человек вписал адрес той же батарейки
    return url or "<без url>"
