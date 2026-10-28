"""Когда сохранять ход агента в хранилище треда."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from gc_core.config_modules.field_meta import gc_meta


class RetrySettings(BaseModel):
    """Повтор записи в хранилище тредов при временной недоступности
    (соединение, таймаут) — с тем же ``op_id``. Паузы растут в
    ``backoff_factor`` раз; сумма должна оставаться меньше срока аренды
    треда (60 с), иначе тред на время повтора отдаётся другому."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    max_attempts: int = Field(
        default=5, ge=1, le=20, json_schema_extra=gc_meta(expert=True)
    )
    initial_delay_sec: float = Field(
        default=1.0, ge=0, json_schema_extra=gc_meta(expert=True)
    )
    backoff_factor: float = Field(
        default=2.0, ge=1, json_schema_extra=gc_meta(expert=True)
    )


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
    #: Как часто идущий ход смотрит входящий поток треда (сообщения с других
    #: процессов и инстансов), секунды.
    inbox_poll_seconds: float = Field(
        default=1.0, gt=0, json_schema_extra=gc_meta(expert=True)
    )
    #: Повтор записи при временной недоступности хранилища.
    retry: RetrySettings = Field(default_factory=RetrySettings)
