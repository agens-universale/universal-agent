from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from gc_core.backends.protocol import (
    DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS,
    DEFAULT_EXEC_OUTPUT_PREVIEW_EDGE_CHARS,
    WALK_OUTPUT_MAX_CHARS,
)

DEFAULT_FINISH_GATE_MAX_ITERATIONS = 10
DEFAULT_SUBAGENT_FINISH_GATE_MAX_ITERATIONS = 3
# Дефолтный таймаут execute_command / run_python_code (секунды).
# Harbor и длинные бенчи поднимают через config (обычно 600).
DEFAULT_EXEC_TIMEOUT_SEC = 120


class LoopDetectorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = True


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    model: str = "MiniMax-M2.7"
    system_prompt_id: str = "v1"
    use_server_subagents: bool = False
    # bool или список имён. Список выбирает КАСТОМНЫХ; встроенные семь доступны
    # всегда, пока значение не false. Пустой список = только встроенные.
    use_local_subagents: bool | list[str] = False
    subagent_max_steps: int = 100
    # Папка с файлами-определениями кастомных сабагентов. None/пусто → папка не
    # читается вовсе и доступны только встроенные классы.
    subagents_dir: str | None = None
    # Предел числа подхватываемых кастомных сабагентов. ge=1: ноль означал бы
    # «папка задана, но агентов нет» — тихая и необъяснимая для оператора ситуация.
    subagents_max_count: int = Field(default=20, ge=1)
    client_name: str = "mini_cli_pub_agent_1"
    x_cl_id: str = "12345"
    # Имя HTTP-заголовка для внешнего trace_id. None → дефолты по назначению:
    # x_trace_id для бэкенда модели, trace_id для MCP. Сам trace_id схемным
    # ключом НЕ является: это свойство вызова, а не конфигурации.
    trace_id_header_name: str | None = None
    # Отправлять ли внешний trace_id в заголовке к бэкенду модели. False
    # (дефолт) — не отправлять: рассылка трейса наружу строго opt-in, как и для
    # MCP-серверов. Гейтит только заголовок; имя лог-файла, записи логов и
    # meta.runtime.trace_ids работают независимо от него.
    trace_header_enabled: bool = False
    verbose: bool = True
    stop_tools: list[str] = Field(default_factory=list)
    # Список canonical id скиллов / паков, которые исключаются из SkillsRegistry
    # при загрузке. Пакет (`pack-id`) выбрасывает и bootstrap, и все inner.
    # Flat-скилл (`skill-id`) выбрасывает один скилл. Дедуп+concat при merge
    # с проектным overlay (project может только усилить запрет).
    disable_skills: list[str] = Field(default_factory=list)
    server_tool_prefix: str = "system"
    max_steps: int = 1000
    # Per-run лимиты надёжности (None = выключено). Область — один ран,
    # как max_steps; сабагент наследует те же значения на свой ран.
    session_token_limit: int | None = None
    max_tool_calls: int | None = None
    # Kill-switch для аварийной LLM-суммаризации трейса при лимите
    # (step_limit / tool_call_limit / token_limit). По умолчанию on.
    # См. docs_md/actual/2026-08-01-limit-trace-summary-spec.md
    limit_trace_summary: bool = True
    no_tool_call_max_count: int = 5
    max_unknown_tool_repeats: int = 5
    strict_turn_finish_validation: bool = True
    finish_gate_max_iterations: int = DEFAULT_FINISH_GATE_MAX_ITERATIONS
    subagent_finish_gate_max_iterations: int = (
        DEFAULT_SUBAGENT_FINISH_GATE_MAX_ITERATIONS
    )
    finish_gate_use_rule_hints: bool = False
    limit_project_metadata: bool = False
    tool_output_inline_max_chars: int = 20000
    stored_tool_outputs_max_entries: int = 50
    # Таймаут execute_command / run_python_code, если модель не передала timeout.
    exec_timeout_sec: int = DEFAULT_EXEC_TIMEOUT_SEC
    # Потолок RAM-буфера stdout/stderr на поток (decoded UTF-8 chars) для exec:
    # L = head floor(L/2) + tail (L - floor(L/2)).
    exec_output_capture_max_chars: int = DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS
    # Размер каждого края front-loaded preview длинного потока (P). 0 — без preview.
    exec_output_preview_edge_chars: int = (
        DEFAULT_EXEC_OUTPUT_PREVIEW_EDGE_CHARS
    )
    # Общий потолок внутренних exec-каналов бэкенда (decoded chars): обход дерева
    # (Harbor: find -printf для walk/listdir), нативный grep, MCP-carrier под os_backend.
    # Переполнение — fail-closed: walk_too_large / McpExecCarrierError, не частичный ответ.
    walk_output_max_chars: int = WALK_OUTPUT_MAX_CHARS
    # Инициализация model-facing exec в Harbor: None = raw, "bashrc" = встроенный
    # profile/bootstrap, любая другая непустая строка = custom shell-префикс.
    harbor_exec_init: str | None = None
    # Синтетические поля tool schema: имя → description. Отсутствует или {} — без инжекта.
    inject_tool_fields: dict[str, str] = Field(default_factory=dict)
    # Steering-текст: при рендере system prompt дописывается в <special_instructions>
    # для любой активной модели (пока непустой). Пусто / omit — без инжекта.
    model_steering: str = ""
    loop_detector: LoopDetectorConfig = Field(
        default_factory=LoopDetectorConfig
    )

    @field_validator("session_token_limit", "max_tool_calls")
    @classmethod
    def validate_optional_positive(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("лимит должен быть None или целым >= 1")
        return value

    @field_validator("trace_id_header_name", mode="before")
    @classmethod
    def normalize_trace_id_header_name(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        for ch in text:
            code = ord(ch)
            if ch.isspace() or code < 0x20 or code == 0x7F or ch == ":":
                raise ValueError(
                    "agent.trace_id_header_name: имя заголовка без пробелов, управляющих символов и ':'"
                )
        return text

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

    @field_validator("exec_output_preview_edge_chars", mode="before")
    @classmethod
    def normalize_exec_output_preview_edge_chars(cls, value: Any) -> int:
        if value is None:
            return DEFAULT_EXEC_OUTPUT_PREVIEW_EDGE_CHARS
        try:
            n = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "agent.exec_output_preview_edge_chars должен быть неотрицательным целым числом"
            ) from exc
        if n < 0:
            raise ValueError(
                "agent.exec_output_preview_edge_chars должен быть неотрицательным целым числом"
            )
        return n

    @model_validator(mode="before")
    @classmethod
    def clamp_preview_edge_to_capture_tail(cls, data: Any) -> Any:
        """P ≤ T = L - floor(L / 2): оба края preview обязаны лежать внутри capture.

        Иначе контракт обещал бы полный короткий канал, уже обрезанный меньшим
        capture-cap. Зажимаем, а не отвергаем: ту же нормализацию делает плоский
        путь, где бросок случился бы уже внутри вызова тулы. До валидации —
        модель неизменяема, и так зажим работает и для конструктора.
        """
        if not isinstance(data, dict):
            return data
        fields = cls.model_fields
        capture = data.get(
            "exec_output_capture_max_chars",
            fields["exec_output_capture_max_chars"].default,
        )
        preview = data.get(
            "exec_output_preview_edge_chars",
            fields["exec_output_preview_edge_chars"].default,
        )
        try:
            capture, preview = int(capture), int(preview)
        except (TypeError, ValueError):
            return data
        capture_tail = capture - capture // 2
        if preview > capture_tail:
            return {**data, "exec_output_preview_edge_chars": capture_tail}
        return data

    @field_validator("model_steering", mode="before")
    @classmethod
    def normalize_model_steering(cls, value: Any) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError("agent.model_steering должен быть строкой")
        return value.strip()

    @field_validator("harbor_exec_init", mode="before")
    @classmethod
    def normalize_harbor_exec_init(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(
                "agent.harbor_exec_init должен быть строкой или null"
            )
        text = value.strip()
        return text or None


def _agent_of(config_dict: Any) -> AgentConfig:
    """Секция ``agent`` из всего, что принимает ``settings_of``."""
    # Ленивый импорт: settings → root → этот модуль.
    from gc_core.settings import settings_of

    return settings_of(config_dict).agent


def exec_output_preview_edge_chars_from_config_dict(
    config_dict: Any,
) -> int:
    """Размер края front-loaded preview (``agent.exec_output_preview_edge_chars``).

    ``P <= T`` схема дожимает сама (``clamp_preview_edge_to_capture_tail``);
    здесь — страховка, чтобы preview не продублировал весь capture.
    """
    agent = _agent_of(config_dict)
    capture = agent.exec_output_capture_max_chars
    return min(agent.exec_output_preview_edge_chars, capture - capture // 2)


def exec_timeout_from_config_dict(config_dict: Any) -> int:
    """Дефолтный exec-timeout (``agent.exec_timeout_sec``; схема гарантирует > 0)."""
    return _agent_of(config_dict).exec_timeout_sec


def exec_output_capture_max_chars_from_config_dict(
    config_dict: Any,
) -> int:
    """Потолок captured stdout/stderr на поток (``agent.exec_output_capture_max_chars``)."""
    return _agent_of(config_dict).exec_output_capture_max_chars


def walk_output_max_chars_from_config_dict(
    config_dict: Any,
) -> int:
    """Потолок приватного канала обхода (Harbor walk/listdir)."""
    n = _agent_of(config_dict).walk_output_max_chars
    return n if n > 0 else WALK_OUTPUT_MAX_CHARS


def harbor_exec_init_from_config_dict(
    config_dict: Any,
) -> str | None:
    """Optional Harbor model-facing shell init (``agent.harbor_exec_init``)."""
    return _agent_of(config_dict).harbor_exec_init


def resolve_model_steering_text(
    config_dict: Any,
    model: str | None = None,
) -> str | None:
    """Текст model_steering или None, если пусто.

    ``model`` зарезервирован для совместимости вызовов; сейчас не используется.
    """
    del model  # API stable; matching by model name may return later
    return _agent_of(config_dict).model_steering.strip() or None


def merge_special_instructions_with_model_steering(
    base: str | None,
    *,
    config_dict: Any = None,
    model: str | None = None,
) -> str | None:
    """Склеить client special_instructions с model_steering из настроек."""
    return join_special_instructions_with_steering(
        base, resolve_model_steering_text(config_dict, model=model)
    )


def join_special_instructions_with_steering(
    base: str | None, steering: str | None
) -> str | None:
    """Склеить special_instructions с уже прочитанным текстом steering."""
    steering = steering.strip() if isinstance(steering, str) else None
    parts: list[str] = []
    if isinstance(base, str) and base.strip():
        parts.append(base.strip())
    if steering:
        parts.append(f"### Model steering\n{steering}")
    return "\n\n".join(parts) if parts else None
