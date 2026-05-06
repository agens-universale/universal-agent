from pydantic import BaseModel, ConfigDict, Field


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
    use_ask_human_tool: bool = False
    stop_tools: list[str] = Field(default_factory=list)
    server_tool_prefix: str = "system"
    max_steps: int = 1000
    no_tool_call_max_count: int = 5
    strict_submit_solution_validation: bool = True
    console_trace: bool = True
    dump_storage: bool = False
    save_logs: bool = True
    limit_project_metadata: bool = False
    tool_output_inline_max_chars: int = 20000
    stored_tool_outputs_max_entries: int = 10
