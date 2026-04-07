"""Tests for subagent registration in build_tools_data."""

import sys
import pytest


@pytest.fixture(autouse=True)
def _ensure_real_tools_module():
    """Восстановить реальный модуль tools, если он был подменён simple_tools/conftest.py."""
    stale = [name for name in sys.modules if name == "tools" or name.startswith("tools.")]
    for name in stale:
        sys.modules.pop(name, None)
    yield
    stale = [name for name in sys.modules if name == "tools" or name.startswith("tools.")]
    for name in stale:
        sys.modules.pop(name, None)


def _base_config(**overrides):
    cfg = {
        "use_local_subagents": False,
        "backend_api_mode": "chat_completions",
        "enabled_tools": [],
        "skills_data_path": "",
        "mcp_servers": [],
    }
    cfg.update(overrides)
    return cfg


class TestSubagentsInBuildToolsData:
    def test_present_when_enabled_and_chat_completions(self):
        from tools import build_tools_data
        from tools.subagents.registry import SUBAGENT_CLASSES
        cfg = _base_config(use_local_subagents=True, backend_api_mode="chat_completions")
        tools_data, tools_spec, tools_descr = build_tools_data(cfg)
        subagent_names = {cls.agent_name for cls in SUBAGENT_CLASSES}
        for name in subagent_names:
            assert name in tools_data, f"{name} should be in tools_data"

    def test_absent_when_disabled(self):
        from tools import build_tools_data
        from tools.subagents.registry import SUBAGENT_CLASSES
        cfg = _base_config(use_local_subagents=False, backend_api_mode="chat_completions")
        tools_data, _, _ = build_tools_data(cfg)
        subagent_names = {cls.agent_name for cls in SUBAGENT_CLASSES}
        for name in subagent_names:
            assert name not in tools_data, f"{name} should NOT be in tools_data"

    def test_absent_when_responses_mode(self):
        from tools import build_tools_data
        from tools.subagents.registry import SUBAGENT_CLASSES
        cfg = _base_config(use_local_subagents=True, backend_api_mode="responses")
        tools_data, _, _ = build_tools_data(cfg)
        subagent_names = {cls.agent_name for cls in SUBAGENT_CLASSES}
        for name in subagent_names:
            assert name not in tools_data, f"{name} should NOT be in tools_data with responses mode"

    def test_absent_when_flag_missing(self):
        from tools import build_tools_data
        from tools.subagents.registry import SUBAGENT_CLASSES
        cfg = _base_config(backend_api_mode="chat_completions")
        del cfg["use_local_subagents"]
        tools_data, _, _ = build_tools_data(cfg)
        subagent_names = {cls.agent_name for cls in SUBAGENT_CLASSES}
        for name in subagent_names:
            assert name not in tools_data

    def test_specs_have_required_field(self):
        from tools import build_tools_data
        from tools.subagents.registry import SUBAGENT_CLASSES
        cfg = _base_config(use_local_subagents=True, backend_api_mode="chat_completions")
        tools_data, _, _ = build_tools_data(cfg)
        subagent_names = {cls.agent_name for cls in SUBAGENT_CLASSES}
        for name in subagent_names:
            spec = tools_data[name]["spec"]
            params = spec.get("parameters", {})
            assert "required" in params and isinstance(params["required"], list), (
                f"{name}: spec.parameters.required must be a list after normalization"
            )

    def test_subagent_specs_in_tools_spec_list(self):
        from tools import build_tools_data
        from tools.subagents.registry import SUBAGENT_CLASSES
        cfg = _base_config(use_local_subagents=True, backend_api_mode="chat_completions")
        _, tools_spec, _ = build_tools_data(cfg)
        spec_names = {s["name"] for s in tools_spec}
        subagent_names = {cls.agent_name for cls in SUBAGENT_CLASSES}
        assert subagent_names.issubset(spec_names)
