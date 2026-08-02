from __future__ import annotations

import logging
import os

from pydantic import BaseModel, ConfigDict, Field

from bundled_assets import bundled_root
from config_modules import env_vars
from config_modules.agent import (
    AgentConfig,
    DEFAULT_FINISH_GATE_MAX_ITERATIONS,
    DEFAULT_SUBAGENT_FINISH_GATE_MAX_ITERATIONS,
)
from config_modules.backend import BackendConfig
from config_modules.logging_config import LoggingConfig
from config_modules.mcp import McpConfig
from config_modules.paths import PathsConfig
from config_modules.security import SecurityConfig
from config_modules.stored_outputs import normalize_tool_output_limits
from config_modules.intent_scope import IntentScopeConfig, normalize_intent_scope_config
from config_modules.quality_gate import QualityGateConfig, normalize_quality_gate_config
from config_modules.working_memory import (
    WorkingMemoryConfig,
    normalize_working_memory_config,
)
from config_modules.message_notice import (
    ToolUsageNoticeRule,
    normalize_tool_usage_notice_rules,
)
from config_modules.project_memory import (
    ProjectMemoryConfig,
    normalize_project_memory_config,
)
from config_modules.chat_completions import ChatCompletionsConfig
from config_modules.pricing import PricingConfig
from config_modules.tui import TuiConfig
from config_modules.update import UpdateConfig
from config_modules.diff_tui import DiffTuiConfig
from config_modules.fs_tools import FsToolsConfig
from config_modules.summary import (
    DEFAULT_INCREMENTAL_BATCH_MULTIPLIER,
    DEFAULT_INTENT_LOG_PREVIEW_CHARS,
    DEFAULT_LAST_DIALOG_STEP_MESSAGE_MAX_CHARS,
    DEFAULT_LAST_DIALOG_STEPS_COUNT,
    DEFAULT_MAX_ACCUMULATED_SUMMARY_CHARS,
    DEFAULT_MAX_DIALOGUE_CHARS,
    DEFAULT_MAX_MESSAGE_CHARS,
    DEFAULT_TURN_END_TRIGGER_RATIO,
    SummaryConfig,
)
from config_modules.strip_tags_projection import (
    StripTagsProjectionConfig,
    normalize_strip_tags_projection_config,
)


class AgentAuthConfig(BaseModel):
    model_config = ConfigDict(extra="allow")


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: BackendConfig = Field(default_factory=BackendConfig)
    chat_completions: ChatCompletionsConfig = Field(default_factory=ChatCompletionsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    summary: SummaryConfig = Field(default_factory=SummaryConfig)
    quality_gate: QualityGateConfig = Field(default_factory=QualityGateConfig)
    intent_scope: IntentScopeConfig = Field(default_factory=IntentScopeConfig)
    working_memory: WorkingMemoryConfig = Field(default_factory=WorkingMemoryConfig)
    tool_usage_notice: list[ToolUsageNoticeRule] = Field(default_factory=list)
    project_memory: ProjectMemoryConfig = Field(default_factory=ProjectMemoryConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    tui: TuiConfig = Field(default_factory=TuiConfig)
    update: UpdateConfig = Field(default_factory=UpdateConfig)
    diff_tui: DiffTuiConfig = Field(default_factory=DiffTuiConfig)
    fs_tools: FsToolsConfig = Field(default_factory=FsToolsConfig)
    strip_tags_projection: StripTagsProjectionConfig = Field(
        default_factory=StripTagsProjectionConfig
    )
    agent_auth: AgentAuthConfig | None = None


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


def resolve_paths(cfg: AppConfig, config_dir: str) -> None:
    def to_abs(path_value: str | None) -> str | None:
        if path_value is None or not str(path_value).strip():
            return None
        path_value = str(path_value).strip()
        if os.path.isabs(path_value):
            return path_value
        return os.path.abspath(os.path.join(config_dir, path_value))

    paths = env_vars.gc_paths()
    slug = env_vars.project_slug()
    project_state = paths["state"] / "projects" / slug

    cfg.paths.agent_md_path = _asset_default(config_dir, cfg.paths.agent_md_path)
    cfg.paths.chat_completions_system_prompt_path = to_abs(
        cfg.paths.chat_completions_system_prompt_path
    ) or _prompt_default(config_dir, "chat_completions_system_prompt.md")
    cfg.paths.chat_completions_intent_guard_prompt_path = to_abs(
        cfg.paths.chat_completions_intent_guard_prompt_path
    ) or _prompt_default(config_dir, "chat_completions_intent_guard_prompt.md")
    cfg.paths.chat_completions_custom_subagent_system_prompt_path = to_abs(
        cfg.paths.chat_completions_custom_subagent_system_prompt_path
    ) or _prompt_default(config_dir, "chat_completions_custom_subagent_system_prompt.md")
    cfg.paths.threads_data_path = (
        to_abs(cfg.paths.threads_data_path) or str(project_state / "threads")
    )
    cfg.paths.skills_data_path = to_abs(cfg.paths.skills_data_path)
    cfg.paths.logs_dump_dir = (
        to_abs(cfg.paths.logs_dump_dir) or str(project_state / "logs")
    )
    cfg.summary.chat_completions_summary_prompt_path = to_abs(
        cfg.summary.chat_completions_summary_prompt_path
    ) or _prompt_default(config_dir, "chat_completions_summary_prompt.md")
    cfg.summary.chat_completions_memory_patch_replace_prompt_path = to_abs(
        cfg.summary.chat_completions_memory_patch_replace_prompt_path
    ) or _prompt_default(config_dir, "chat_completions_memory_patch_replace.md")
    if cfg.summary.intent_log_preview_chars <= 0:
        cfg.summary.intent_log_preview_chars = DEFAULT_INTENT_LOG_PREVIEW_CHARS
    if cfg.summary.chat_completions_summary_trigger_chars < 0:
        cfg.summary.chat_completions_summary_trigger_chars = 0
    turn_end_raw = cfg.summary.chat_completions_summary_turn_end_trigger_chars
    if turn_end_raw is None:
        trigger = cfg.summary.chat_completions_summary_trigger_chars
        cfg.summary.chat_completions_summary_turn_end_trigger_chars = (
            max(1, int(trigger * DEFAULT_TURN_END_TRIGGER_RATIO))
            if trigger > 0
            else 0
        )
    elif turn_end_raw <= 0:
        cfg.summary.chat_completions_summary_turn_end_trigger_chars = 0
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
        cfg.summary.max_accumulated_summary_chars = DEFAULT_MAX_ACCUMULATED_SUMMARY_CHARS
    if cfg.summary.incremental_batch_chars is None or cfg.summary.incremental_batch_chars <= 0:
        trigger = cfg.summary.chat_completions_summary_trigger_chars
        cfg.summary.incremental_batch_chars = (
            int(trigger * DEFAULT_INCREMENTAL_BATCH_MULTIPLIER) if trigger > 0 else 0
        )
    normalize_quality_gate_config(cfg.quality_gate)
    normalize_working_memory_config(cfg.working_memory)
    cfg.tool_usage_notice = normalize_tool_usage_notice_rules(cfg.tool_usage_notice)
    normalize_project_memory_config(cfg.project_memory)
    normalize_intent_scope_config(cfg.intent_scope)
    normalize_strip_tags_projection_config(cfg.strip_tags_projection)
    cfg.intent_scope.intent_scope_tool_summary_prompt_path = to_abs(
        cfg.intent_scope.intent_scope_tool_summary_prompt_path
    ) or _prompt_default(config_dir, "intent_scope_tool_summary_prompt.md")
    if cfg.agent.finish_gate_max_iterations < 1:
        logging.getLogger(__name__).warning(
            "finish_gate_reject_limit_invalid finish_gate_max_iterations=%s; using %s",
            cfg.agent.finish_gate_max_iterations,
            DEFAULT_FINISH_GATE_MAX_ITERATIONS,
        )
        cfg.agent.finish_gate_max_iterations = DEFAULT_FINISH_GATE_MAX_ITERATIONS
    if cfg.agent.subagent_finish_gate_max_iterations < 1:
        logging.getLogger(__name__).warning(
            "finish_gate_reject_limit_invalid subagent_finish_gate_max_iterations=%s; using %s",
            cfg.agent.subagent_finish_gate_max_iterations,
            DEFAULT_SUBAGENT_FINISH_GATE_MAX_ITERATIONS,
        )
        cfg.agent.subagent_finish_gate_max_iterations = (
            DEFAULT_SUBAGENT_FINISH_GATE_MAX_ITERATIONS
        )
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
