"""Values for the native executor workflow and its message handlers."""

from dataclasses import dataclass, field
from typing import Any

from gc_core.context_preparation import PreparedRunContext
from gc_core.execution.contracts import ModelResult
from gc_core.execution.guard_contracts import GuardProbeResult
from gc_core.execution.resource_contracts import ResourceOwnerRef
from gc_core.run_preparation import PreparedPrompts, PromptPreparationRequest
from gc_core.tools.subagents.custom.file_definitions import SubagentDefinition

#: Предел текста сообщения в байтах UTF-8. Текст целиком идёт во вход
#: update и при первой отправке — во вход activity модели; запись истории
#: ограничена 2 МБ, и 1 МБ оставляет место промпту и остальному входу.
#: Считаем байты, а не символы: миллион символов кириллицы — 2 МБ.
MAX_MESSAGE_BYTES = 1_000_000


@dataclass
class UserMessage:
    message_id: str
    text: str

    def validate(self):
        if (
            not self.message_id
            or len(self.message_id) > 128
            or not self.text.strip()
            or len(self.text.encode("utf-8", "surrogatepass"))
            > MAX_MESSAGE_BYTES
        ):
            raise ValueError(
                "Message requires an ID <= 128 chars and nonempty text "
                f"<= {MAX_MESSAGE_BYTES} bytes (UTF-8)"
            )


@dataclass
class SessionResume:
    """Delivery receipts across Continue-As-New; agent state stays in its store."""

    human_replies: dict[str, str] = field(default_factory=dict)
    reply_receipts: dict[str, list[str]] = field(default_factory=dict)
    formatted_answer: str | None = None
    post_error: str | None = None
    closed: bool = False
    undelivered: list[dict[str, str]] = field(default_factory=list)
    accepted: dict[str, str] = field(default_factory=dict)
    pending: list[dict[str, str]] = field(default_factory=list)
    turns: int = 0
    answer: Any = None
    terminal_outcome: str | None = None
    schema_version: int = 1
    #: Сообщения, обработанные завершёнными ходами: ID сообщения → номер
    #: хода (``wait_for_message`` переживает Continue-As-New).
    completed: dict[str, int] = field(default_factory=dict)
    #: Владение тредом уже подтверждено прошлым run: перечитывать не нужно.
    store_owner_epoch: int | None = None
    store_generation: int | None = None


@dataclass(frozen=True)
class RunRequest:
    profile: str
    store_id: str
    system_id: str
    thread_id: str
    messages: list[str]
    #: Очереди activities: ``None`` — очередь самого workflow (как у Pydantic
    #: AI: один воркер на одной очереди обслуживает всё).
    resource_queue: str | None = None
    model_queue: str | None = None
    message_ids: list[str] | None = None
    schema_version: int = 1
    format_text: str | None = None
    keep_alive: bool = False
    resume: SessionResume | None = None


@dataclass(frozen=True)
class ActivityCall:
    profile: str
    owner_id: str
    operation_id: str
    data: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1
    resource_owner: ResourceOwnerRef | None = None
    child_context: dict[str, Any] | None = None


@dataclass
class PreparedContext:
    context: PreparedRunContext
    tool_metadata: dict[str, dict[str, Any]]
    store_capabilities: list[str]
    threads_dir: str
    resource_owner: ResourceOwnerRef | None = None
    model_transport_configured: bool = False
    provider_currency: str = "$"
    schema_version: int = 1
    subagent_definitions: dict[str, SubagentDefinition] = field(
        default_factory=dict
    )
    #: Предвычислено на worker'е той же activity: экономит две activity на ход.
    #: Используется, только если запрос ядра совпал с предвычисленным.
    skills: list[dict] | None = None
    prompts_request: PromptPreparationRequest | None = None
    prompts: PreparedPrompts | None = None
    #: Таймауты и повторы activities воркера (``activity_config``).
    activity_settings: dict[str, Any] = field(default_factory=dict)


@dataclass
class GuardProbes:
    """Факты guard-проверки и попутно вычисленные факты следующей проверки."""

    primary: GuardProbeResult
    companions: list[GuardProbeResult] = field(default_factory=list)


@dataclass
class ModelReply:
    result: ModelResult
    circuit_breaker: dict[str, str] = field(default_factory=dict)
