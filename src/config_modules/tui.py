from pydantic import BaseModel, ConfigDict


class TuiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    show_reasoning_line: bool = True   # живая строка-мысль ⠇ … (на счётчик/индикатор не влияет)
    show_stream_answer: bool = False   # живой текст ответа (серый стрим)
