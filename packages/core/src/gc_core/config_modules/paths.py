from pydantic import BaseModel, ConfigDict, Field

from gc_core.config_modules.field_meta import gc_meta


class PathsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    agent_md_path: str = ""
    chat_completions_system_prompt_path: str | None = Field(
        default=None, json_schema_extra=gc_meta(expert=True)
    )
    chat_completions_intent_guard_prompt_path: str | None = Field(
        default=None, json_schema_extra=gc_meta(expert=True)
    )
    # Инструкция судьи пары (auto_routing_safe_level: auto). None →
    # prompts/chat_completions_routing_pair_judge_prompt.md рядом с конфигом либо bundled.
    chat_completions_routing_pair_judge_prompt_path: str | None = Field(
        default=None, json_schema_extra=gc_meta(expert=True)
    )
    # Промпт для кастомных (файловых) сабагентов. None → prompts/
    # chat_completions_custom_subagent_system_prompt.md рядом с конфигом либо bundled.
    chat_completions_custom_subagent_system_prompt_path: str | None = Field(
        default=None, json_schema_extra=gc_meta(expert=True)
    )
    chat_completions_special_instructions_path: str | None = Field(
        default=None, json_schema_extra=gc_meta(expert=True)
    )
    # restart: привязывается при старте TUI — hot-reload не подхватит.
    threads_data_path: str | None = Field(
        default=None, json_schema_extra=gc_meta(expert=True, restart=True)
    )
    skills_data_path: str | None = Field(
        default=None, json_schema_extra=gc_meta(expert=True)
    )
    # restart: логгер рана создаётся при старте.
    logs_dump_dir: str | None = Field(
        default=None, json_schema_extra=gc_meta(expert=True, restart=True)
    )
