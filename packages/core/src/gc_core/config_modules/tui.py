from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from gc_core.config_modules.field_meta import gc_meta
from gc_core.config_modules.finish_gate_mode import FinishGateMode


class BangShellConfig(BaseModel):
    """Yaml ``tui.bang_shell`` — локальный `!command` в TUI (не агент)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    # None / omit / <=0 = без лимита. Положительное N = авто-kill через N секунд.
    timeout_sec: Optional[float] = Field(
        default=None, json_schema_extra=gc_meta(expert=True)
    )
    max_output_bytes: int = 32768
    max_output_lines: int = 200


class FileViewConfig(BaseModel):
    """Yaml ``tui.file_view`` — постраничный `/view` в TUI."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    max_file_bytes: int = 64 * 1024 * 1024
    # None = авто по высоте экрана; число = фиксированный шаг PgUp/PgDn (1..200).
    page_lines: Optional[int] = Field(
        default=None, json_schema_extra=gc_meta(expert=True)
    )


class TuiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

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
    # Тема TUI: auto — фон от терминала (при отсутствии ответа на запрос
    # или светлом фоне — gigacode); gigacode — собственная тёмная палитра.
    theme: Literal["auto", "gigacode"] = "auto"
    # adaptive bounce: как поступать с текст-ответом без тула.
    # deny — всегда bounce (как CLI); judge — судья intent_guard решает;
    # allow — всегда принять текст (ни bounce, ни судьи).
    text_answer_mode: Literal["deny", "judge", "allow"] = "judge"
    # /gate в палитре: always — судья на turn_finish всегда; off — не зовётся;
    # smart — судья только если за ход был >=1 вызов инструмента (спека §3).
    # Только TUI; CLI/server рулит agent.strict_turn_finish_validation.
    finish_gate_mode: FinishGateMode = "smart"
    # Только TUI: оставить лишь корневой AGENTS.md (L3), отбросив global
    # (~/.config/gc) и .gc/AGENTS.md. Игнорируется, если задан
    # paths.agent_md_path (путь главнее флага).
    agentsmd_override: bool = False
    bang_shell: BangShellConfig = Field(
        default_factory=BangShellConfig,
        json_schema_extra=gc_meta(worker_only=True),
    )
    file_view: FileViewConfig = Field(
        default_factory=FileViewConfig,
        json_schema_extra=gc_meta(worker_only=True),
    )
