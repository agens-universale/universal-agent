from typing import Literal

from pydantic import BaseModel, ConfigDict


class TuiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    show_reasoning_line: bool = True   # живая строка-мысль ⠇ … (на счётчик/индикатор не влияет)
    show_stream_answer: bool = False   # живой текст ответа (серый стрим)
    # adaptive bounce: как поступать с текст-ответом без тула.
    # strict — всегда bounce (как CLI); judge — судья intent_guard решает;
    # always — всегда принять текст (ни bounce, ни судьи).
    text_answer_mode: Literal["strict", "judge", "always"] = "judge"
