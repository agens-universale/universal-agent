"""stop_tools режет слитый набор, не только builtin."""
from gc_core.tools import build_tools_data
from gc_core.tools.subagents.custom.file_subagent import REQUIRED_TOOL_NAMES


def _mcp_entry(name: str) -> dict:
    return {
        "spec": {
            "type": "function",
            "name": name,
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        "description": name,
    }


def test_stop_tools_removes_skills_and_mcp_from_spec(monkeypatch):
    def fake_mcp(_config_dict, on_progress=None, os_backend=None):
        return (
            {"mcp_jira_search": _mcp_entry("mcp_jira_search")},
            {"jira": {"status": "connected"}},
        )

    monkeypatch.setattr("gc_core.tools.get_mcp_tools_data", fake_mcp)
    registered: list[str] = []
    cfg = {
        "stop_tools": ["skills_list", "mcp_jira_search"],
        "backend_api_mode": "responses",
    }
    data, spec, _, _ = build_tools_data(cfg, registered_names=registered)
    spec_names = {item.get("name") for item in spec}

    assert "skills_list" not in data
    assert "skills_list" not in spec_names
    assert "skills_load" in data
    assert "mcp_jira_search" not in data
    assert "mcp_jira_search" not in spec_names
    # Каталог до вычитания: успешно снятое имя не опечатка.
    assert "skills_list" in registered
    assert "mcp_jira_search" in registered


def test_global_stop_is_not_restored_by_subagent_core(monkeypatch):
    monkeypatch.setattr(
        "gc_core.tools.get_mcp_tools_data",
        lambda *_a, **_k: ({}, {}),
    )
    cfg = {
        "stop_tools": ["turn_finish"],
        "backend_api_mode": "responses",
        "subagent_tools_allow": ("fs_read_file", *REQUIRED_TOOL_NAMES),
    }
    data, spec, _, _ = build_tools_data(cfg)
    spec_names = {item.get("name") for item in spec}
    assert "turn_finish" not in data
    assert "turn_finish" not in spec_names
    assert "fs_read_file" in data
