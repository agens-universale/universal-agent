"""stop_tools режет слитый набор, не только builtin.

build_tools_data импортируется внутри тестов: autouse-фикстура из
test_builtin_tool_names.py чистит gc_core.tools* из sys.modules, поэтому
модульный импорт держал бы устаревший объект, а monkeypatch по строке
патчил бы заново импортированный модуль — фейк MCP не срабатывал.
"""

from gc_core.tools.subagents.custom.file_subagent import REQUIRED_TOOL_NAMES
from gc_testkit import run_coro
from gc_testkit.settings import make_settings


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
    async def fake_mcp(
        _config_dict, on_progress=None, tools_cache=None, os_backend=None
    ):
        return (
            {"mcp_jira_search": _mcp_entry("mcp_jira_search")},
            {"jira": {"status": "connected"}},
        )

    from gc_core.tools import abuild_tools_data

    monkeypatch.setattr(
        "gc_core.tools.connect_mcp.agent_tools.aget_mcp_tools_data_for_config",
        fake_mcp,
    )
    registered: list[str] = []
    cfg = {
        "stop_tools": ["skills_list", "mcp_jira_search"],
        "backend_api_mode": "responses",
    }
    data, spec, _, _ = run_coro(
        abuild_tools_data(make_settings(cfg), registered_names=registered)
    )
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
    from gc_core.tools import abuild_tools_data

    async def _no_mcp(*_a, **_k):
        return ({}, {})

    monkeypatch.setattr(
        "gc_core.tools.connect_mcp.agent_tools.aget_mcp_tools_data_for_config",
        _no_mcp,
    )
    cfg = {
        "stop_tools": ["turn_finish"],
        "backend_api_mode": "responses",
        "subagent_tools_allow": ("fs_read_file", *REQUIRED_TOOL_NAMES),
    }
    data, spec, _, _ = run_coro(abuild_tools_data(make_settings(cfg)))
    spec_names = {item.get("name") for item in spec}
    assert "turn_finish" not in data
    assert "turn_finish" not in spec_names
    assert "fs_read_file" in data
