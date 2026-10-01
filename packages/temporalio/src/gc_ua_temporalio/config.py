"""Bounded deployment profiles. Unsupported core options are rejected."""

from pathlib import Path
from typing import Literal

from gc_core.execution.tool_policy import GUARDED_TOOLS
from pydantic import BaseModel, ConfigDict, Field, field_validator

from gc_ua_temporalio.mcp_config import McpServerProfile

DEFAULT_TOOLS = (
    "fs_read_file",
    "session_memory",
    "session_todo",
    "exec_calculator",
    "exec_sleep",
    "turn_intermediate_reply",
    "turn_finish",
    "session_read_stored_tool_output",
)
SUPPORTED_TOOLS = (
    *DEFAULT_TOOLS,
    *sorted(GUARDED_TOOLS),
    "fs_file_info",
    "fs_list_files",
    "fs_find_files_by_name",
    "fs_find_in_files",
    "session_compact",
)


class AgentProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    revision: str = "v1"
    workspace: str
    model: str
    completions_url: str
    api_key_env: str | None = None
    tools: list[str] = Field(default_factory=lambda: list(DEFAULT_TOOLS))
    max_steps: int = Field(default=20, ge=1, le=50)
    max_tokens: int = Field(default=4096, ge=256, le=32000)
    request_timeout_seconds: int = Field(default=90, ge=1, le=180)
    inline_output_chars: int = Field(default=8000, ge=256, le=16000)
    system_prompt: str = "You are Universal Agent. Use the available tools. Finish with turn_finish."
    text_answer_mode: Literal["deny"] = "deny"
    strict_turn_finish_validation: Literal[False] = False
    command_moderation_mode: Literal["ask", "deny"] = "ask"
    summary_trigger_chars: int = Field(default=60000, ge=64, le=120000)
    summary_trigger_tokens: int = Field(default=16000, ge=0, le=64000)
    mcp_servers: list[str] = Field(default_factory=list)
    subagents: dict[str, str] = Field(default_factory=dict)
    preview_stream: bool = False
    max_tool_calls: int = Field(default=200, ge=1, le=500)
    session_token_limit: int = Field(default=200000, ge=1, le=2000000)

    @field_validator("tools")
    @classmethod
    def supported(cls, value):
        unsupported = set(value) - set(SUPPORTED_TOOLS)
        if unsupported:
            raise ValueError(
                f"Temporal profile does not support: {sorted(unsupported)}"
            )
        if (
            "turn_finish" not in value
            or "session_read_stored_tool_output" not in value
        ):
            raise ValueError(
                "turn_finish and session_read_stored_tool_output are required"
            )
        return value

    def core_config(self):
        return {
            "backend_api_mode": "chat_completions",
            "model": self.model,
            "chat_completions_max_tokens": self.max_tokens,
            "chat_completions_min_response_tokens": 256,
            "backend_request_timeout_sec": self.request_timeout_seconds,
            "use_retry": False,
            "use_max_tokens_param": True,
            "strict_turn_finish_validation": self.strict_turn_finish_validation,
            "text_answer_mode": self.text_answer_mode,
            "no_tool_call_max_count": 3,
            "max_tool_calls": self.max_tool_calls,
            "session_token_limit": self.session_token_limit,
            "tool_output_inline_max_chars": self.inline_output_chars,
            "limit_trace_summary": False,
            "gc": {"force_collect": False},
            "loop_detector": {"enabled": True},
            "console_trace": False,
            "command_moderation": {
                "enabled": True,
                "mode": self.command_moderation_mode,
            },
        }


class TemporalRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    address: str = "localhost:7233"
    namespace: str = "default"
    workflow_queue: str = "ua-workflow"
    resource_queue: str = "ua-resource-local"
    model_queue: str = "ua-model"
    database: str
    artifacts: str
    profiles: dict[str, AgentProfile]
    mcp_servers: dict[str, McpServerProfile] = Field(default_factory=dict)
    mcp_host_port: int = Field(default=7335, ge=1024, le=65535)
    mcp_host_token_env: str | None = None
    mcp_lease_seconds: int = Field(default=300, ge=30, le=86400)

    @classmethod
    def load(cls, path):
        return cls.model_validate_json(Path(path).read_text())
