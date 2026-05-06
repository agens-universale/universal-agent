from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, Field

from config_modules.agent import AgentConfig
from config_modules.backend import BackendConfig
from config_modules.mcp import McpConfig
from config_modules.paths import PathsConfig
from config_modules.security import SecurityConfig
from config_modules.stored_outputs import normalize_tool_output_limits
from config_modules.summary import (
    DEFAULT_INCREMENTAL_BATCH_MULTIPLIER,
    DEFAULT_LAST_DIALOG_STEP_MESSAGE_MAX_CHARS,
    DEFAULT_LAST_DIALOG_STEPS_COUNT,
    DEFAULT_MAX_ACCUMULATED_SUMMARY_CHARS,
    DEFAULT_MAX_DIALOGUE_CHARS,
    DEFAULT_MAX_MESSAGE_CHARS,
    DEFAULT_ORIGINAL_USER_MESSAGES_MAX_CHARS,
    SummaryConfig,
)


class AgentAuthConfig(BaseModel):
    model_config = ConfigDict(extra="allow")


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: BackendConfig = Field(default_factory=BackendConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    summary: SummaryConfig = Field(default_factory=SummaryConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    agent_auth: AgentAuthConfig | None = None


def resolve_paths(cfg: AppConfig, config_dir: str) -> None:
    def to_abs(path_value: str | None) -> str | None:
        if path_value is None:
            return None
        if os.path.isabs(path_value):
            return path_value
        return os.path.abspath(os.path.join(config_dir, path_value))

    cfg.paths.agent_md_path = to_abs(cfg.paths.agent_md_path) or cfg.paths.agent_md_path
    cfg.paths.chat_completions_system_prompt_path = to_abs(
        cfg.paths.chat_completions_system_prompt_path
    ) or os.path.join(config_dir, "prompts", "chat_completions_system_prompt.md")
    cfg.paths.threads_data_path = to_abs(cfg.paths.threads_data_path)
    cfg.paths.skills_data_path = to_abs(cfg.paths.skills_data_path)
    cfg.paths.logs_dump_dir = to_abs(cfg.paths.logs_dump_dir)
    cfg.summary.chat_completions_summary_prompt_path = to_abs(
        cfg.summary.chat_completions_summary_prompt_path
    ) or os.path.join(config_dir, "prompts", "chat_completions_summary_prompt.md")
    if cfg.summary.chat_completions_summary_trigger_chars < 0:
        cfg.summary.chat_completions_summary_trigger_chars = 0
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
    if cfg.summary.original_user_messages_max_chars <= 0:
        cfg.summary.original_user_messages_max_chars = (
            DEFAULT_ORIGINAL_USER_MESSAGES_MAX_CHARS
        )
    if cfg.summary.incremental_batch_chars is None or cfg.summary.incremental_batch_chars <= 0:
        trigger = cfg.summary.chat_completions_summary_trigger_chars
        cfg.summary.incremental_batch_chars = (
            int(trigger * DEFAULT_INCREMENTAL_BATCH_MULTIPLIER) if trigger > 0 else 0
        )
    (
        cfg.agent.tool_output_inline_max_chars,
        cfg.agent.stored_tool_outputs_max_entries,
    ) = normalize_tool_output_limits(
        cfg.agent.tool_output_inline_max_chars,
        cfg.agent.stored_tool_outputs_max_entries,
    )
    cfg.backend.completions_url = str(cfg.backend.completions_url).strip()
    root_box = cfg.security.command_moderation.root_box
    root_box.extra_write_paths = [
        to_abs(path) or path for path in root_box.extra_write_paths
    ]
