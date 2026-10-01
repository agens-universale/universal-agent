"""Freeze MCP discovery in the bootstrap activity using the existing normalizer."""

from gc_core.tools.connect_mcp.agent_tools import normalize_tool_spec_for_agent
from gc_core.tools.connect_mcp.registry import _normalize_tool

from gc_ua_temporalio.mcp_rpc import McpHostClient


async def discover_mcp(runtime, request):
    profile, _ = runtime.profile(
        request.start.profile, request.start.profile_revision
    )
    metadata = {}
    if not profile.mcp_servers:
        return metadata
    host = McpHostClient(runtime.config)
    for server in profile.mcp_servers:
        discovered = await host.request(
            "acquire",
            owner=request.owner,
            profile=request.start.profile,
            revision=request.start.profile_revision,
            server=server,
        )
        reference = {
            k: discovered[k]
            for k in ("session_id", "generation", "server", "revision")
        }
        for raw in discovered["tools"]:
            normalized = _normalize_tool(
                server,
                raw,
                transport="",
                url="",
                server_display_name=server,
                server_description="",
            )
            if normalized is None:
                continue
            spec = normalize_tool_spec_for_agent(normalized["_spec"])
            name = spec["name"]
            if len(name) > 64 or name in metadata:
                raise ValueError("Ambiguous or oversized MCP tool name")
            metadata[name] = {
                "spec": spec,
                "description": spec["description"],
                "mcp": reference,
                "mcp_tool_name": raw["name"],
                "mcp_schema": raw.get("inputSchema", {}),
                "execution": {"kind": "mcp", "retry": "host_receipt"},
            }
    if len(metadata) > 64:
        raise ValueError(
            "Temporal MCP profile permits at most 64 discovered tools"
        )
    return metadata
