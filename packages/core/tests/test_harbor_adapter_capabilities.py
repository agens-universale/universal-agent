"""GcCliExternal passes Harbor's agent-capability gate (harbor >= 0.24).

Harbor 0.24 added `AgentCapabilities` and refuses to start an agent on a task
with MCP servers or skills unless the agent declares them. Older harbor has no
gate, so the adapter must import there as before.
"""

import sys
from types import SimpleNamespace

import pytest
from gc_testkit.paths import HARBOR

if str(HARBOR) not in sys.path:
    sys.path.insert(0, str(HARBOR))

from adapters.gc_cli_external import GcCliExternal


def test_adapter_imports_without_capability_api():
    # On harbor < 0.24 the declaration must be skipped, not crash the import.
    try:
        import harbor.agents.capabilities  # noqa: F401
    except ImportError:
        assert "capabilities" not in GcCliExternal.__dict__
    else:
        assert GcCliExternal.capabilities.mcp_servers


def test_harbor_capability_gate_accepts_mcp_and_skills_task(tmp_path):
    pytest.importorskip("harbor.agents.capabilities")
    from harbor.models.task.config import MCPServerConfig
    from harbor.trial.trial import Trial

    servers = [
        MCPServerConfig(name="user-server", url="http://user-server:8000/mcp")
    ]
    agent = GcCliExternal(
        logs_dir=tmp_path,
        model_name="openai/m",
        mcp_servers=servers,
        skills_dir="/skills",
    )
    # Just the attributes Harbor's own validator reads (0.24 and 0.25 shapes),
    # so the check runs Harbor's code, not a copy of its rule.
    trial = SimpleNamespace(
        agent=agent,
        user_agent=None,
        agent_environment=SimpleNamespace(os=None),
        _effective_skills_dir="/skills",
        config=SimpleNamespace(
            agent=SimpleNamespace(name=agent.name(), skills=[], mcp_servers=[])
        ),
        task=SimpleNamespace(
            config=SimpleNamespace(
                environment=SimpleNamespace(
                    mcp_servers=servers, skills_dir="/skills"
                )
            )
        ),
    )

    Trial._validate_agent_capabilities(trial)
