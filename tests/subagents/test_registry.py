"""Tests for tools.subagents.registry"""

from tools.subagents.registry import (
    SUBAGENT_CLASSES,
    get_subagent_class,
    get_subagents_tools_data,
    is_subagent_tool,
)
from tools.subagents.worker import Worker
from tools.subagents.coder import CodeGenerator
from tools.subagents.planner import Planner
from tools.subagents.critic import Critic
from tools.subagents.code_reviewer import CodeReviewer
from tools.subagents.special_agent import SpecialAgent


class TestIsSubagentTool:
    def test_known_names(self):
        assert is_subagent_tool("subagent_worker") is True
        assert is_subagent_tool("subagent_code_generator") is True
        assert is_subagent_tool("subagent_planner") is True
        assert is_subagent_tool("subagent_critic") is True
        assert is_subagent_tool("subagent_code_reviewer") is True
        assert is_subagent_tool("system_subagent_special_agent") is True

    def test_unknown_names(self):
        assert is_subagent_tool("read_file") is False
        assert is_subagent_tool("write_to_file") is False
        assert is_subagent_tool("subagent_nonexistent") is False
        assert is_subagent_tool("") is False


class TestGetSubagentClass:
    def test_returns_correct_class(self):
        assert get_subagent_class("subagent_worker") is Worker
        assert get_subagent_class("subagent_code_generator") is CodeGenerator
        assert get_subagent_class("subagent_planner") is Planner
        assert get_subagent_class("subagent_critic") is Critic
        assert get_subagent_class("subagent_code_reviewer") is CodeReviewer
        assert get_subagent_class("system_subagent_special_agent") is SpecialAgent

    def test_unknown_returns_none(self):
        assert get_subagent_class("unknown_tool") is None
        assert get_subagent_class("") is None


class TestGetSubagentsToolsData:
    def test_returns_dict_with_all_subagents(self):
        data = get_subagents_tools_data()
        assert isinstance(data, dict)
        assert len(data) == len(SUBAGENT_CLASSES)

    def test_each_entry_has_required_structure(self):
        data = get_subagents_tools_data()
        for name, entry in data.items():
            assert "spec" in entry, f"{name}: missing 'spec'"
            assert "description" in entry, f"{name}: missing 'description'"
            assert "tool_fabric" in entry, f"{name}: missing 'tool_fabric'"
            assert entry["tool_fabric"] is None

    def test_each_spec_has_required_fields(self):
        data = get_subagents_tools_data()
        for name, entry in data.items():
            spec = entry["spec"]
            assert "name" in spec, f"{name}: spec missing 'name'"
            assert "parameters" in spec, f"{name}: spec missing 'parameters'"
            assert "description" in spec, f"{name}: spec missing 'description'"
            assert spec["name"] == name

    def test_keys_match_agent_names(self):
        data = get_subagents_tools_data()
        expected_names = {cls.agent_name for cls in SUBAGENT_CLASSES}
        assert set(data.keys()) == expected_names
