from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class BangShellConfig(BaseModel):
    """Yaml ``tui.bang_shell`` — локальный `!command` в TUI (не агент)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    # None / omit / <=0 = без лимита. Положительное N = авто-kill через N секунд.
    timeout_sec: Optional[float] = None
    max_output_bytes: int = 32768
    max_output_lines: int = 200


class FileViewConfig(BaseModel):
    """Yaml ``tui.file_view`` — постраничный `/view` в TUI."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_file_bytes: int = 64 * 1024 * 1024
    # None = авто по высоте экрана; число = фиксированный шаг PgUp/PgDn (1..200).
    page_lines: Optional[int] = None


class TuiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Мастер-свитч live-стриминга в TUI (chat_completions). На CLI/server/swarm
    # НЕ влияет — там всегда non-stream (стрим гейтится наличием on_stream_delta,
    # который ставит только TUI). None = не задан → legacy-фолбэк на backend.stream.
    stream: Optional[bool] = None
    # Что рендерить во время стрима (только chat_completions + stream:true;
    # на счётчик токенов/индикатор не влияет — только контент):
    #   minimal (default) — индикатор + счётчик, без текста стрима;
    #   answer            — прогрессивный ответ, дорастающий до финала;
    #   answer_reasoning  — + однострочный «глазок» рассуждения «⠇ …».
    stream_display: Literal["minimal", "answer", "answer_reasoning"] = (
        "minimal"
    )
    # adaptive bounce: как поступать с текст-ответом без тула.
    # deny — всегда bounce (как CLI); judge — судья intent_guard решает;
    # allow — всегда принять текст (ни bounce, ни судьи).
    text_answer_mode: Literal["deny", "judge", "allow"] = "judge"
    # /gate в палитре: on — судья на turn_finish всегда; off — не зовётся;
    # smart — судья только если за ход был >=1 вызов инструмента (спека §3).
    # Только TUI; CLI/server рулит agent.strict_turn_finish_validation.
    finish_gate_mode: Literal["on", "off", "smart"] = "smart"
    # Только TUI: оставить лишь корневой AGENTS.md (L3), отбросив global
    # (~/.config/gc) и .gc/AGENTS.md. Игнорируется, если задан
    # paths.agent_md_path (путь главнее флага).
    agentsmd_override: bool = False
    bang_shell: BangShellConfig = Field(default_factory=BangShellConfig)
    file_view: FileViewConfig = Field(default_factory=FileViewConfig)
