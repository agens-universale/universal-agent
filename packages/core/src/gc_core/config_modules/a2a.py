"""Yaml ``a2a`` — порт и режимы процесса gc-a2a."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from gc_core.config_modules.field_meta import gc_meta


class A2aConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Слушает этот процесс. По умолчанию только loopback.
    host: str = "127.0.0.1"
    port: int = Field(default=8091, ge=1, le=65535)
    # URL в карточке (supportedInterfaces.url). None — собрать из host:port.
    # Нужен, когда bind на 0.0.0.0/:: : пир по адресу сокета не ходит.
    public_url: str | None = None
    # Пустая строка — заголовок Authorization не проверяется.
    auth_token: str = Field(default="", json_schema_extra=gc_meta(secret=True))
    # cwd нового диалога, если в сообщении нет абсолютного metadata.cwd.
    launch_root: str | None = None
    # name карточки.
    name: str = "GigaCode"
    # description карточки и текст скилла.
    description: str = "Универсальный агент GigaCode"
    # Текстовый ответ без тула. Дефолт канала — allow: проза принимается.
    text_answer_mode: Literal["deny", "judge", "allow"] = "allow"
    # Судья на turn_finish. off — strict_turn_finish_validation становится false.
    finish_gate_mode: Literal["on", "off", "smart"] = "off"
    # Каждые N шагов без session_todo — technical reminder. 0 = выкл.
    todo_reminder_steps: int = Field(default=0, ge=0)

    @field_validator("host")
    @classmethod
    def _host(cls, value: str) -> str:
        text = value.strip()
        if text == "":
            raise ValueError("a2a.host должен быть непустой строкой")
        return text

    @field_validator("public_url", mode="before")
    @classmethod
    def _public_url(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("a2a.public_url должен быть строкой или null")
        text = value.strip()
        if text == "":
            return None
        if not text.startswith(("http://", "https://")):
            raise ValueError(
                "a2a.public_url должен начинаться с http:// или https://"
            )
        return text

    @field_validator("auth_token", mode="before")
    @classmethod
    def _token(cls, value: object) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError("a2a.auth_token должен быть строкой")
        return value

    @field_validator("launch_root", mode="before")
    @classmethod
    def _launch_root(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("a2a.launch_root должен быть строкой или null")
        text = value.strip()
        return text or None

    @field_validator("name", "description")
    @classmethod
    def _text(cls, value: str) -> str:
        if value.strip() == "":
            raise ValueError("a2a.name и a2a.description не бывают пустыми")
        return value
