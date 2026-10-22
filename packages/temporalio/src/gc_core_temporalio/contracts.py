"""Values for the native executor workflow and its message handlers."""

from dataclasses import dataclass, field
from typing import Any

from gc_core.auto_routing.request_equivalence import PreparedRequestEquivalence
from gc_core.context_preparation import PreparedRunContext
from gc_core.execution.contracts import ModelResult
from gc_core.execution.guard_contracts import GuardProbeResult
from gc_core.execution.resource_contracts import ResourceOwnerRef
from gc_core.run_preparation import PreparedPrompts, PromptPreparationRequest
from gc_core.tools.subagents.custom.file_definitions import SubagentDefinition

from gc_core_temporalio.sizes import payload_size

#: Предел текста сообщения в байтах истории Temporal (``payload_size``:
#: не-ASCII символ — 6 байт, кириллица — примерно 166 тысяч символов на
#: мегабайт). Текст целиком идёт во вход update и при первой отправке — во
#: вход activity модели; запись истории ограничена 2 МБ.
MAX_MESSAGE_BYTES = 1_000_000


@dataclass
class UserMessage:
    message_id: str
    text: str
    #: Отпечаток настроек приложения (``settings_fingerprint``): расходится с
    #: уже сверенным у сессии — сообщение отклоняется (``SettingsMismatch``).
    settings_fingerprint: dict[str, str] | None = None

    def validate(self):
        if (
            not self.message_id
            or len(self.message_id) > 128
            or not self.text.strip()
            or payload_size(self.text) > MAX_MESSAGE_BYTES
        ):
            raise ValueError(
                "Message requires an ID <= 128 chars and nonempty text "
                f"<= {MAX_MESSAGE_BYTES} bytes in Temporal history "
                "(non-ASCII characters take 6 bytes each)"
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
    #: хода (``message_state`` переживает Continue-As-New).
    completed: dict[str, int] = field(default_factory=dict)
    #: Итоги последних ходов по номеру (``message_state``: ответ своего хода).
    turn_outcomes: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Владение тредом уже подтверждено прошлым run: перечитывать не нужно.
    store_owner_epoch: int | None = None
    store_generation: int | None = None
    #: Отпечаток настроек, принятый из сообщений (``settings_fingerprint``).
    message_fingerprint: dict[str, str] | None = None
    #: ``answer``/``formatted_answer``/``post_error`` — те же, что в итоге
    #: последнего хода: в resume не дублируются (берутся оттуда).
    answer_in_outcome: bool = False
    #: Ответ для ``status``/результата укорочен (полный — в треде).
    answer_truncated: bool = False


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
    #: Отпечаток настроек приложения (``settings_fingerprint``): воркер
    #: сверяет его со своим профилем на подготовке хода. ``None`` — без
    #: сверки (CLI, JSON-клиенты).
    settings_fingerprint: dict[str, str] | None = None


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
    #: Есть ли человек, который ответит (``WorkerConfig.human_input``): нет —
    #: запрос к человеку сразу получает отказ, а не вечное ожидание.
    human_input: bool = True
    #: Отношение main-запросов, вычисленное до публичной проекции настроек.
    request_equivalence: PreparedRequestEquivalence | None = None


@dataclass
class GuardProbes:
    """Факты guard-проверки и попутно вычисленные факты следующей проверки."""

    primary: GuardProbeResult
    companions: list[GuardProbeResult] = field(default_factory=list)


@dataclass
class ModelReply:
    result: ModelResult
    circuit_breaker: dict[str, str] = field(default_factory=dict)
