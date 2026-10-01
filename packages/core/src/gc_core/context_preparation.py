"""Prepared context values and runtime dependencies for the single core factory.

Preparation owns filesystem/discovery/credentials. Assembly consumes its values
and explicit runtime bindings, without creating clients, logs, or workspaces.
The durable adapter supplies its public configuration snapshot separately from
the credential-bearing worker configuration.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PreparedRunContext:
    config_dict: dict[str, Any]
    run_id: str
    logging_path: str
    summary_trace_path: str
    trace_id: str
    project_gc_dir: str
    workspace_root: str
    workspace_shell: str
    workspace_os_name: str
    workspace_root_writable: bool
    tools_spec: list[dict[str, Any]]
    tools_description: list[str]
    mcp_status: dict[str, dict[str, Any]]
    registered_tool_names: list[str]
    mcp_session_owner: str
    is_tui: bool = False
    schema_version: int = 1

    @classmethod
    def from_context(cls, ctx, *, config_dict):
        """Describe a prepared native context without its live dependencies.

        The caller must explicitly choose the configuration being exported;
        durable runtimes supply their public policy snapshot here.
        """
        return cls(
            config_dict=config_dict,
            run_id=ctx.run_id,
            logging_path=ctx.logging_path,
            summary_trace_path=ctx.summary_trace_path,
            trace_id=ctx.trace_id,
            project_gc_dir=str(ctx.project_gc_dir),
            workspace_root=ctx.workspace.root,
            workspace_shell=ctx.workspace.shell,
            workspace_os_name=ctx.workspace.os_name,
            workspace_root_writable=ctx.workspace.root_writable,
            tools_spec=ctx.tools_spec,
            tools_description=ctx.tools_description,
            mcp_status=ctx.mcp_status,
            registered_tool_names=sorted(ctx.registered_tool_names),
            mcp_session_owner=ctx.mcp_session_owner,
            is_tui=getattr(ctx, "is_tui", False),
        )


@dataclass
class RunContextBindings:
    """Live dependencies; this object is never serialized into workflow history."""

    os_backend: Any
    skills_registry: Any
    logger: Any
    tools_data: dict[str, dict[str, Any]]
    model_router: Any
    auth_connector: Any = None
    environment: Any = None
    thread_store: Any = None
    tools_cache: Any = None
    on_stream_delta: Any = None
    on_messages_evicted: Any = None
    operations: Any = None
    prompt_loader: Any = None
    state_preparer: Any = None
    human_request: Any = None
    guard_probe: Any = None
    tool_effect: Any = None
    # Runtime-specific lifecycle hooks, not alternative agent policies.
    lock_factory: Any = None
    skills_loader: Any = None
    mcp_cleanup: Any = None
    prompt_resource: Any = None
    project_memory_io: Any = None
    project_memory_hash: Any = None
    summary_trace_sink: Any = None
    collect_garbage: Any = None
