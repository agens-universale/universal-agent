from __future__ import annotations

import logging
import os

from pydantic import BaseModel, ConfigDict, Field, field_validator

from gc_core.bundled_assets import bundled_root
from gc_core.config_modules import env_vars
from gc_core.config_modules.agent import (
    DEFAULT_FINISH_GATE_MAX_ITERATIONS,
    DEFAULT_SUBAGENT_FINISH_GATE_MAX_ITERATIONS,
    AgentConfig,
)
from gc_core.config_modules.backend import BackendConfig
from gc_core.config_modules.chat_completions import ChatCompletionsConfig
from gc_core.config_modules.diff_tui import DiffTuiConfig
from gc_core.config_modules.endpoint_presets import (
    EndpointPreset,
    default_endpoint_presets,
)
from gc_core.config_modules.fs_tools import FsToolsConfig
from gc_core.config_modules.intent_scope import (
    IntentScopeConfig,
    normalize_intent_scope_config,
)
from gc_core.config_modules.logging_config import LoggingConfig
from gc_core.config_modules.mcp import McpConfig
from gc_core.config_modules.message_notice import (
    ToolUsageNoticeRule,
    normalize_tool_usage_notice_rules,
)
from gc_core.config_modules.model_registry import (
    AssignConfig,
    ModelEntry,
    SlotsConfig,
    validate_level_value,
    validate_model_alias,
)
from gc_core.config_modules.paths import PathsConfig
from gc_core.config_modules.pricing import PricingConfig
from gc_core.config_modules.project_memory import (
    ProjectMemoryConfig,
    normalize_project_memory_config,
)
from gc_core.config_modules.quality_gate import (
    QualityGateConfig,
    normalize_quality_gate_config,
)
from gc_core.config_modules.security import SecurityConfig
from gc_core.config_modules.stored_outputs import normalize_tool_output_limits
from gc_core.config_modules.strip_tags_projection import (
    StripTagsProjectionConfig,
    normalize_strip_tags_projection_config,
)
from gc_core.config_modules.summary import (
    DEFAULT_INCREMENTAL_BATCH_MULTIPLIER,
    DEFAULT_INTENT_LOG_PREVIEW_CHARS,
    DEFAULT_LAST_DIALOG_STEP_MESSAGE_MAX_CHARS,
    DEFAULT_LAST_DIALOG_STEPS_COUNT,
    DEFAULT_MAX_ACCUMULATED_SUMMARY_CHARS,
    DEFAULT_MAX_DIALOGUE_CHARS,
    DEFAULT_MAX_MESSAGE_CHARS,
    DEFAULT_TURN_END_TRIGGER_RATIO,
    SUMMARY_CHARS_PER_TOKEN,
    SummaryConfig,
)
from gc_core.config_modules.todo import TodoConfig, normalize_todo_config
from gc_core.config_modules.tui import TuiConfig
from gc_core.config_modules.update import UpdateConfig
from gc_core.config_modules.voice import VoiceConfig
from gc_core.config_modules.voice_endpoint_presets import (
    default_voice_endpoint_presets,
)
from gc_core.config_modules.working_memory import (
    WorkingMemoryConfig,
    normalize_working_memory_config,
)


class AgentAuthConfig(BaseModel):
    model_config = ConfigDict(extra="allow")


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: BackendConfig = Field(default_factory=BackendConfig)
    chat_completions: ChatCompletionsConfig = Field(
        default_factory=ChatCompletionsConfig
    )
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    summary: SummaryConfig = Field(default_factory=SummaryConfig)
    quality_gate: QualityGateConfig = Field(default_factory=QualityGateConfig)
    intent_scope: IntentScopeConfig = Field(default_factory=IntentScopeConfig)
    working_memory: WorkingMemoryConfig = Field(
        default_factory=WorkingMemoryConfig
    )
    todo: TodoConfig = Field(default_factory=TodoConfig)
    tool_usage_notice: list[ToolUsageNoticeRule] = Field(default_factory=list)
    project_memory: ProjectMemoryConfig = Field(
        default_factory=ProjectMemoryConfig
    )
    paths: PathsConfig = Field(default_factory=PathsConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    tui: TuiConfig = Field(default_factory=TuiConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    endpoint_presets: list[EndpointPreset] = Field(
        default_factory=default_endpoint_presets
    )
    voice_endpoint_presets: list[EndpointPreset] = Field(
        default_factory=default_voice_endpoint_presets
    )
    update: UpdateConfig = Field(default_factory=UpdateConfig)
    diff_tui: DiffTuiConfig = Field(default_factory=DiffTuiConfig)
    fs_tools: FsToolsConfig = Field(default_factory=FsToolsConfig)
    strip_tags_projection: StripTagsProjectionConfig = Field(
        default_factory=StripTagsProjectionConfig
    )
    agent_auth: AgentAuthConfig | None = None

    # Профили v2 (волна A, фаза 1): реестр моделей + уровни + привязки + указатели.
    # Ссылочная целостность (слоты/привязки против merged-реестра) — фаза 2.
    profile: str = ""
    level: str = "default"
    models: dict[str, ModelEntry] = Field(default_factory=dict)
    slots: SlotsConfig = Field(default_factory=SlotsConfig)
    assign: AssignConfig = Field(default_factory=AssignConfig)

    @field_validator("level")
    @classmethod
    def _validate_level(cls, value: str) -> str:
        return validate_level_value(value)

    @field_validator("models")
    @classmethod
    def _validate_model_aliases(
        cls, value: dict[str, ModelEntry]
    ) -> dict[str, ModelEntry]:
        for alias in value:
            validate_model_alias(alias)
        return value


def _prompt_default(config_dir: str, name: str) -> str:
    """config_dir/prompts/<name> если существует, иначе bundled_root()/prompts/<name>."""
    local = os.path.join(config_dir, "prompts", name)
    if os.path.isfile(local):
        return local
    return os.path.join(str(bundled_root()), "prompts", name)


def _asset_default(config_dir: str, rel_path: str) -> str:
    """config_dir/<rel_path> если файл существует; иначе bundled_root()/<rel_path>
    (для относительных asset-путей вроде agent_md_path). Абсолютные/пустые — как есть."""
    if not rel_path or os.path.isabs(rel_path):
        return rel_path
    local = os.path.abspath(os.path.join(config_dir, rel_path))
    if os.path.isfile(local):
        return local
    bundled = os.path.normpath(os.path.join(str(bundled_root()), rel_path))
    if os.path.isfile(bundled):
        return bundled
    return local


def _to_abs(config_dir: str, path_value: str | None) -> str | None:
    if path_value is None or not str(path_value).strip():
        return None
    path_value = str(path_value).strip()
    if os.path.isabs(path_value):
        return path_value
    return os.path.abspath(os.path.join(config_dir, path_value))


def _normalize_summary_limits(cfg: AppConfig) -> None:
    """Нули/отрицательные лимиты summary → дефолты; производные триггеры — от основного."""
    if cfg.summary.intent_log_preview_chars <= 0:
        cfg.summary.intent_log_preview_chars = DEFAULT_INTENT_LOG_PREVIEW_CHARS
    cfg.summary.chat_completions_summary_trigger_chars = (
        _clamp_summary_trigger(
            cfg.summary.chat_completions_summary_trigger_chars
        )
    )
    cfg.summary.chat_completions_summary_turn_end_trigger_chars = (
        _resolve_turn_end_trigger(
            cfg.summary.chat_completions_summary_turn_end_trigger_chars,
            cfg.summary.chat_completions_summary_trigger_chars,
        )
    )
    cfg.summary.chat_completions_summary_trigger_tokens = (
        _clamp_summary_trigger(
            cfg.summary.chat_completions_summary_trigger_tokens
        )
    )
    cfg.summary.chat_completions_summary_turn_end_trigger_tokens = (
        _resolve_turn_end_trigger(
            cfg.summary.chat_completions_summary_turn_end_trigger_tokens,
            cfg.summary.chat_completions_summary_trigger_tokens,
        )
    )
    if cfg.summary.last_dialog_steps_count <= 0:
        cfg.summary.last_dialog_steps_count = DEFAULT_LAST_DIALOG_STEPS_COUNT
    if cfg.summary.last_dialog_step_message_max_chars <= 0:
        cfg.summary.last_dialog_step_message_max_chars = (
            DEFAULT_LAST_DIALOG_STEP_MESSAGE_MAX_CHARS
        )
    if cfg.summary.max_message_chars <= 0:
        cfg.summary.max_message_chars = DEFAULT_MAX_MESSAGE_CHARS
    if cfg.summary.max_dialogue_chars <= 0:
        cfg.summary.max_dialogue_chars = DEFAULT_MAX_DIALOGUE_CHARS
    if cfg.summary.max_accumulated_summary_chars <= 0:
        cfg.summary.max_accumulated_summary_chars = (
            DEFAULT_MAX_ACCUMULATED_SUMMARY_CHARS
        )
    if (
        cfg.summary.incremental_batch_chars is None
        or cfg.summary.incremental_batch_chars <= 0
    ):
        trigger = (
            cfg.summary.chat_completions_summary_trigger_chars
            or cfg.summary.chat_completions_summary_trigger_tokens
            * SUMMARY_CHARS_PER_TOKEN
        )
        cfg.summary.incremental_batch_chars = (
            int(trigger * DEFAULT_INCREMENTAL_BATCH_MULTIPLIER)
            if trigger > 0
            else 0
        )


def _normalize_finish_gate_limits(cfg: AppConfig) -> None:
    if cfg.agent.finish_gate_max_iterations < 1:
        logging.getLogger(__name__).warning(
            "finish_gate_reject_limit_invalid finish_gate_max_iterations=%s; using %s",
            cfg.agent.finish_gate_max_iterations,
            DEFAULT_FINISH_GATE_MAX_ITERATIONS,
        )
        cfg.agent.finish_gate_max_iterations = (
            DEFAULT_FINISH_GATE_MAX_ITERATIONS
        )
    if cfg.agent.subagent_finish_gate_max_iterations < 1:
        logging.getLogger(__name__).warning(
            "finish_gate_reject_limit_invalid subagent_finish_gate_max_iterations=%s; using %s",
            cfg.agent.subagent_finish_gate_max_iterations,
            DEFAULT_SUBAGENT_FINISH_GATE_MAX_ITERATIONS,
        )
        cfg.agent.subagent_finish_gate_max_iterations = (
            DEFAULT_SUBAGENT_FINISH_GATE_MAX_ITERATIONS
        )


def resolve_paths(cfg: AppConfig, config_dir: str) -> None:
    def to_abs(path_value: str | None) -> str | None:
        return _to_abs(config_dir, path_value)

    paths = env_vars.gc_paths()
    slug = env_vars.project_slug()
    project_state = paths["state"] / "projects" / slug

    cfg.paths.agent_md_path = _asset_default(
        config_dir, cfg.paths.agent_md_path
    )
    cfg.paths.chat_completions_system_prompt_path = to_abs(
        cfg.paths.chat_completions_system_prompt_path
    ) or _prompt_default(config_dir, "chat_completions_system_prompt.md")
    cfg.paths.chat_completions_intent_guard_prompt_path = to_abs(
        cfg.paths.chat_completions_intent_guard_prompt_path
    ) or _prompt_default(config_dir, "chat_completions_intent_guard_prompt.md")
    cfg.paths.chat_completions_custom_subagent_system_prompt_path = to_abs(
        cfg.paths.chat_completions_custom_subagent_system_prompt_path
    ) or _prompt_default(
        config_dir, "chat_completions_custom_subagent_system_prompt.md"
    )
    cfg.paths.threads_data_path = to_abs(cfg.paths.threads_data_path) or str(
        project_state / "threads"
    )
    cfg.paths.skills_data_path = to_abs(cfg.paths.skills_data_path)
    cfg.paths.logs_dump_dir = to_abs(cfg.paths.logs_dump_dir) or str(
        project_state / "logs"
    )
    cfg.summary.chat_completions_summary_prompt_path = to_abs(
        cfg.summary.chat_completions_summary_prompt_path
    ) or _prompt_default(config_dir, "chat_completions_summary_prompt.md")
    cfg.summary.chat_completions_memory_patch_replace_prompt_path = to_abs(
        cfg.summary.chat_completions_memory_patch_replace_prompt_path
    ) or _prompt_default(
        config_dir, "chat_completions_memory_patch_replace.md"
    )
    _normalize_summary_limits(cfg)
    normalize_quality_gate_config(cfg.quality_gate)
    normalize_working_memory_config(cfg.working_memory)
    normalize_todo_config(cfg.todo)
    cfg.tool_usage_notice = normalize_tool_usage_notice_rules(
        cfg.tool_usage_notice
    )
    normalize_project_memory_config(cfg.project_memory)
    normalize_intent_scope_config(cfg.intent_scope)
    normalize_strip_tags_projection_config(cfg.strip_tags_projection)
    cfg.intent_scope.intent_scope_tool_summary_prompt_path = to_abs(
        cfg.intent_scope.intent_scope_tool_summary_prompt_path
    ) or _prompt_default(config_dir, "intent_scope_tool_summary_prompt.md")
    _normalize_finish_gate_limits(cfg)
    (
        cfg.agent.tool_output_inline_max_chars,
        cfg.agent.stored_tool_outputs_max_entries,
    ) = normalize_tool_output_limits(
        cfg.agent.tool_output_inline_max_chars,
        cfg.agent.stored_tool_outputs_max_entries,
    )
    cfg.backend.completions_url = str(cfg.backend.completions_url).strip()
    write_guard = cfg.security.write_guard
    write_guard.extra_paths = [
        to_abs(path) or path for path in write_guard.extra_paths
    ]


def _clamp_summary_trigger(value: int) -> int:
    return 0 if value < 0 else value


def _resolve_turn_end_trigger(raw: int | None, mid: int) -> int:
    """Нет ключа — 60% от mid-turn этой же шкалы. Явный 0 и отрицательное — плечо выкл."""
    if raw is None:
        return (
            max(1, int(mid * DEFAULT_TURN_END_TRIGGER_RATIO)) if mid > 0 else 0
        )
    return 0 if raw <= 0 else raw


# Вводная текста ошибки схемы — ОДНА строка на всех пишущих и на разбор
# (`config_origin.error_keys` снимает её перед поиском координат: иначе первый
# сегмент склейки прятал бы свой ключ за вводной, и виновник терялся).
SCHEMA_ERROR_PREFIX = "конфиг не проходит схему: "

# Та же роль у отказа схемы ФАЙЛА ПРОФИЛЯ (`config_modules.profiles.
# _validate_profile_document`) — своя вводная, не `SCHEMA_ERROR_PREFIX`: профиль
# не конфиг целиком. `error_keys` обязана снимать ОБЕ (ревью MR #80, #3) — снимая
# только первую, она прятала первый ключ профильной ошибки за этой вводной.
PROFILE_SCHEMA_ERROR_PREFIX = "профиль не проходит схему: "


def schema_error_details(exc) -> str:
    """Текст ошибки схемы из `exc.errors()`: координата `loc` + причина `msg`,
    БЕЗ pydantic-дампа. `str(ValidationError)` несёт `input_value=<repr записи>`
    — на правилах `ModelEntry` это набранный или лежащий в файле токен (ревью
    MR #80, L28). Одна редакция на всех показывающих: `layers._app_config_from_
    raw`, `profiles._validate_profile_document`, `onboarding.explain_broken_
    config` — написанная трижды, склейка разъезжалась бы молча."""
    # `removeprefix("Value error, ")`: правила-валидаторы бросают ValueError с
    # ПОЛНЫМ русским текстом, а pydantic кладёт его в msg, приписав спереди тип
    # по-английски — служебный огрызок посреди русской фразы человеку не нужен.
    # У ошибок полей ("Input should be …") префикса нет, они не меняются.
    #
    # `removeprefix("models.<alias>: ")` следом: правила `ModelEntry` несут ЭТУ
    # координату в СВОЁМ тексте буквально («models.<alias>» — placeholder, не
    # подставленный алиас, «одна редакция на всех» правило владельца), а pydantic
    # приписывает свою из `loc` («models.opus41: …») — без снятия склейка называла
    # координату дважды (ревью MR #80, #4). Тексты правил не меняем — они матчатся
    # тестами; снимается только этот повтор, здесь же.
    return "; ".join(
        f"{'.'.join(map(str, err['loc']))}: {err['msg'].removeprefix('Value error, ').removeprefix('models.<alias>: ')}"
        for err in exc.errors()
    )
