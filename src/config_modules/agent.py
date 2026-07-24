from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backends.protocol import DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS

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
    # Потолок RAM-буфера stdout/stderr на поток (decoded UTF-8 chars) для exec.
    exec_output_capture_max_chars: int = DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS
    # Синтетические поля tool schema: имя → description. Отсутствует или {} — без инжекта.
    inject_tool_fields: dict[str, str] = Field(default_factory=dict)
    # Steering-текст: при рендере system prompt дописывается в <special_instructions>
    # для любой активной модели (пока непустой). Пусто / omit — без инжекта.
    model_steering: str = ""

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

    @field_validator("exec_output_capture_max_chars", mode="before")
    @classmethod
    def normalize_exec_output_capture_max_chars(cls, value: Any) -> int:
        if value is None:
            return DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS
        try:
            n = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "agent.exec_output_capture_max_chars должен быть неотрицательным целым числом"
            ) from exc
        if n < 0:
            raise ValueError(
                "agent.exec_output_capture_max_chars должен быть неотрицательным целым числом"
            )
        return n

    @field_validator("model_steering", mode="before")
    @classmethod
    def normalize_model_steering(cls, value: Any) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError("agent.model_steering должен быть строкой")
        return value.strip()


def exec_timeout_from_config_dict(config_dict: dict[str, Any] | None) -> int:
    """Дефолтный exec-timeout из плоского config_dict (после flatten_app_config)."""
    raw = (config_dict or {}).get("exec_timeout_sec", DEFAULT_EXEC_TIMEOUT_SEC)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_EXEC_TIMEOUT_SEC
    return n if n > 0 else DEFAULT_EXEC_TIMEOUT_SEC


def exec_output_capture_max_chars_from_config_dict(
    config_dict: dict[str, Any] | None,
) -> int:
    """Потолок captured stdout/stderr на поток из плоского config_dict."""
    raw = (config_dict or {}).get(
        "exec_output_capture_max_chars", DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS
    )
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS
    return n if n >= 0 else DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS


def resolve_model_steering_text(
    config_dict: dict[str, Any] | None,
    model: str | None = None,
) -> str | None:
    """Текст model_steering или None, если пусто.

    ``model`` зарезервирован для совместимости вызовов; сейчас не используется.
    """
    del model  # API stable; matching by model name may return later
    cfg = config_dict or {}
    raw = cfg.get("model_steering")
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text or None


def merge_special_instructions_with_model_steering(
    base: str | None,
    *,
    config_dict: dict[str, Any] | None = None,
    model: str | None = None,
) -> str | None:
    """Склеить client special_instructions с model_steering из config_dict."""
    steering = resolve_model_steering_text(config_dict, model=model)
    parts: list[str] = []
    if isinstance(base, str) and base.strip():
        parts.append(base.strip())
    if steering:
        parts.append(f"### Model steering\n{steering}")
    return "\n\n".join(parts) if parts else None
