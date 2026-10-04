"""Конфиг секции ``images:`` — картинки во вводе и сколько их уходит модели пикселями."""

from __future__ import annotations

from typing import Any, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)


class ImagesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Картинок в одном сообщении: больше — отказ при вставке (TUI), ошибка старта (CLI).
    max_per_message: int = Field(default=10, ge=1)
    # Сколько картинок человека (самых свежих, в последних LIVE_USER_TURNS ходах)
    # уходит модели пикселями; остальные — запиской «вне контекста».
    live_user_images: int = 10
    # Сколько снимков агента (fs_view_image, только текущий ход) уходит пикселями.
    # Не меньше 1: при 0 тул прикреплял бы снимок, которого модель не увидит, и звался снова.
    live_tool_images: int = Field(default=3, ge=1)

    @model_validator(mode="after")
    def _newest_message_stays_whole(self) -> ImagesConfig:
        # Самое свежее сообщение живо целиком: иначе картинка, которую человек
        # только что вставил, ушла бы модели запиской.
        if self.live_user_images < self.max_per_message:
            raise ValueError(
                "images.live_user_images must be >= images.max_per_message"
            )
        return self


def read_images_config(config_dict: Mapping[str, Any] | None) -> ImagesConfig:
    """Секция ``images`` из ``config_dict`` — единственный путь чтения во всём коде.

    Зовётся на каждом запросе, поэтому не бросает никогда: секции нет (двойники
    ctx, словарь собран руками мимо ``AppConfig``) или она невалидна — дефолты,
    как у соседних читателей секций (``parse_find_in_files_config``,
    ``intent_scope_from_config_dict``).
    """
    raw = (config_dict or {}).get("images")
    if not isinstance(raw, Mapping):
        return ImagesConfig()
    try:
        return ImagesConfig.model_validate(dict(raw))
    except ValidationError:
        return ImagesConfig()
