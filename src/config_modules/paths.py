from pydantic import BaseModel, ConfigDict


class PathsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_md_path: str = ""
    chat_completions_system_prompt_path: str | None = None
    chat_completions_intent_guard_prompt_path: str | None = None
    chat_completions_special_instructions_path: str | None = None
    threads_data_path: str | None = None
    skills_data_path: str | None = None
    logs_dump_dir: str | None = None
