from typing import Literal

from pydantic import BaseModel, ConfigDict


class TuiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Что рендерить во время стрима (только chat_completions + backend.stream:true;
    # на счётчик токенов/индикатор не влияет — только контент):
    #   minimal          — индикатор + счётчик, без текста стрима;
    #   answer (default) — прогрессивный ответ, дорастающий до финала;
    #   answer_reasoning — + однострочный «глазок» рассуждения «⠇ …».
    stream_display: Literal["minimal", "answer", "answer_reasoning"] = "answer"
    # adaptive bounce: как поступать с текст-ответом без тула.
    # strict — всегда bounce (как CLI); judge — судья intent_guard решает;
    # always — всегда принять текст (ни bounce, ни судьи).
    text_answer_mode: Literal["strict", "judge", "always"] = "judge"
