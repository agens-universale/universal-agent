"""Yaml ``chat_api`` — порт и режимы процесса gc-chat."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ChatApiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Слушает этот процесс. По умолчанию только loopback.
    host: str = "127.0.0.1"
    port: int = Field(default=8090, ge=1, le=65535)
    # Пустая строка — заголовок Authorization не проверяется.
    auth_token: str = ""
    # Пустой список — CORS выключен: API доступен только своей странице и
    # не-браузерным клиентам. Непустой — только эти origin. ``*`` из списка
    # выбрасывается: любой сайт в браузере запускал бы агента с shell.
    cors_origins: list[str] = Field(default_factory=list)
    # Рабочий каталог нового хода, если в запросе нет абсолютного metadata.cwd.
    launch_root: str | None = None
    # Текстовый ответ без тула. Дефолт канала — allow: проза принимается.
    text_answer_mode: Literal["deny", "judge", "allow"] = "allow"
    # Судья на turn_finish. off — strict_turn_finish_validation становится false.
    finish_gate_mode: Literal["on", "off", "smart"] = "off"
    # Каждые N шагов без session_todo — technical reminder. 0 = выкл.
    # Тул остаётся. В этом канале чеклист пользователю не показывается.
    todo_reminder_steps: int = Field(default=0, ge=0)

    @field_validator("host")
    @classmethod
    def _host(cls, value: str) -> str:
        text = value.strip()
        if text == "":
            raise ValueError("chat_api.host должен быть непустой строкой")
        return text

    @field_validator("auth_token", mode="before")
    @classmethod
    def _token(cls, value: object) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError("chat_api.auth_token должен быть строкой")
        return value

    @field_validator("cors_origins")
    @classmethod
    def _origins(cls, value: list[str]) -> list[str]:
        return [item for item in value if item != "*"]

    @field_validator("launch_root", mode="before")
    @classmethod
    def _launch_root(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(
                "chat_api.launch_root должен быть строкой или null"
            )
        text = value.strip()
        return text or None
