"""Yaml ``acp`` — настройки канала Agent Client Protocol (gc-acp)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AcpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Как поступать с текстовым ответом без tool-call в IDE-чате.
    # По умолчанию allow: обсуждение и правка доки — нормальный сценарий ACP.
    text_answer_mode: Literal["deny", "judge", "allow"] = "allow"
    # Судья на turn_finish: off — не звать; smart — только если за ход был tool;
    # on — всегда. В IDE человек сам видит ответ/дифф и сверяет с интентом.
    finish_gate_mode: Literal["on", "off", "smart"] = "off"
    # Каждые N шагов без session_todo — technical reminder. 0 = выкл.
    # В IDE-чате чек-лист чаще толкает к «делай код» вопреки обсуждению.
    todo_reminder_steps: int = Field(default=0, ge=0)
    # Ожидание ответа IDE на session/request_permission. 0 — сразу deny_once.
    confirm_timeout_sec: float = Field(default=600, ge=0)
