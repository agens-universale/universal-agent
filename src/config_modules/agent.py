from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_FINISH_GATE_MAX_ITERATIONS = 10
DEFAULT_SUBAGENT_FINISH_GATE_MAX_ITERATIONS = 3
# Дефолтный таймаут execute_command / run_python_code (секунды).
# Harbor и длинные бенчи поднимают через config (обычно 600).
DEFAULT_EXEC_TIMEOUT_SEC = 120


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = "MiniMax-M2.7"
    system_prompt_id: str = "v1"
    use_server_subagents: bool = False
    use_local_subagents: bool = False
    subagent_max_steps: int = 100
    client_name: str = "mini_cli_pub_agent_1"
    x_cl_id: str = "12345"
    verbose: bool = True
    stop_tools: list[str] = Field(default_factory=list)
    # Список canonical id скиллов / паков, которые исключаются из SkillsRegistry
    # при загрузке. Пакет (`pack-id`) выбрасывает и bootstrap, и все inner.
    # Flat-скилл (`skill-id`) выбрасывает один скилл. Дедуп+concat при merge
    # с проектным overlay (project может только усилить запрет).
    disable_skills: list[str] = Field(default_factory=list)
    server_tool_prefix: str = "system"
    max_steps: int = 1000
    no_tool_call_max_count: int = 5
    max_unknown_tool_repeats: int = 5
    strict_turn_finish_validation: bool = True
    finish_gate_max_iterations: int = DEFAULT_FINISH_GATE_MAX_ITERATIONS
    subagent_finish_gate_max_iterations: int = DEFAULT_SUBAGENT_FINISH_GATE_MAX_ITERATIONS
    finish_gate_use_rule_hints: bool = False
    limit_project_metadata: bool = False
    tool_output_inline_max_chars: int = 20000
    stored_tool_outputs_max_entries: int = 50
    # Таймаут execute_command / run_python_code, если модель не передала timeout_seconds.
    exec_timeout_sec: int = DEFAULT_EXEC_TIMEOUT_SEC
    # Синтетические поля tool schema: имя → description. Отсутствует или {} — без инжекта.
    inject_tool_fields: dict[str, str] = Field(default_factory=dict)

    @field_validator("exec_timeout_sec", mode="before")
    @classmethod
    def normalize_exec_timeout_sec(cls, value: Any) -> int:
        if value is None:
            return DEFAULT_EXEC_TIMEOUT_SEC
        try:
            n = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "agent.exec_timeout_sec должен быть положительным целым числом"
            ) from exc
        if n <= 0:
            raise ValueError(
                "agent.exec_timeout_sec должен быть положительным целым числом"
            )
        return n


def exec_timeout_from_config_dict(config_dict: dict[str, Any] | None) -> int:
    """Дефолтный exec-timeout из плоского config_dict (после flatten_app_config)."""
    raw = (config_dict or {}).get("exec_timeout_sec", DEFAULT_EXEC_TIMEOUT_SEC)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_EXEC_TIMEOUT_SEC
    return n if n > 0 else DEFAULT_EXEC_TIMEOUT_SEC
