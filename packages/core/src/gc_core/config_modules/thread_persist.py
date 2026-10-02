"""Когда сохранять ход агента в хранилище треда."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from gc_core.config_modules.field_meta import gc_meta


class ThreadStoreSettings(BaseModel):
    """``persist``: ``step`` — после каждого шага и в конце хода (как всегда);
    ``turn`` — шаги копятся и пишутся в точках сброса: конец хода, перед
    компакцией, перед ожиданием человека, при stop/ошибке.

    ``turn`` допустим только в durable-исполнителе (Temporal): там шаги до
    сброса защищены историей workflow. Локально без неё они потерялись бы при
    падении процесса, поэтому локальный прогон ``turn`` отклоняет.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    persist: Literal["step", "turn"] = Field(
        default="step", json_schema_extra=gc_meta(expert=True)
    )
