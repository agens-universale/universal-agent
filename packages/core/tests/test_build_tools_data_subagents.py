"""Tests for subagent registration in abuild_tools_data."""

import sys

import pytest
from gc_testkit.settings import make_settings


@pytest.fixture(autouse=True)
def _ensure_real_tools_module():
    """Восстановить реальный модуль tools, если он был подменён tests/tools/conftest.py."""
    stale = [
        name
        for name in sys.modules
        if name == "gc_core.tools" or name.startswith("gc_core.tools.")
    ]
    for name in stale:
        sys.modules.pop(name, None)
    yield
    stale = [
        name
        for name in sys.modules
        if name == "gc_core.tools" or name.startswith("gc_core.tools.")
    ]
    for name in stale:
        sys.modules.pop(name, None)


def _base_config(**overrides):
    cfg = {
        "use_local_subagents": False,
        "backend_api_mode": "chat_completions",
        "skills_data_path": "",
    }
    cfg.update(overrides)
    return cfg


class TestSubagentsInBuildToolsData:
    async def test_present_when_enabled_and_chat_completions(self):
        from gc_core.tools import abuild_tools_data
        from gc_core.tools.subagents.registry import SUBAGENT_CLASSES

        cfg = _base_config(
            use_local_subagents=True, backend_api_mode="chat_completions"
        )
        tools_data, tools_spec, tools_descr, _ = await abuild_tools_data(
            make_settings(cfg)
        )
        subagent_names = {cls.agent_name for cls in SUBAGENT_CLASSES}
        for name in subagent_names:
            assert name in tools_data, f"{name} should be in tools_data"

    async def test_absent_when_disabled(self):
        from gc_core.tools import abuild_tools_data
        from gc_core.tools.subagents.registry import SUBAGENT_CLASSES

        cfg = _base_config(
            use_local_subagents=False, backend_api_mode="chat_completions"
        )
        tools_data, _, _, _ = await abuild_tools_data(make_settings(cfg))
        subagent_names = {cls.agent_name for cls in SUBAGENT_CLASSES}
        for name in subagent_names:
            assert name not in tools_data, (
                f"{name} should NOT be in tools_data"
            )

    async def test_absent_when_responses_mode(self):
        from gc_core.tools import abuild_tools_data
        from gc_core.tools.subagents.registry import SUBAGENT_CLASSES

        cfg = _base_config(
            use_local_subagents=True, backend_api_mode="responses"
        )
        tools_data, _, _, _ = await abuild_tools_data(make_settings(cfg))
        subagent_names = {cls.agent_name for cls in SUBAGENT_CLASSES}
        for name in subagent_names:
            assert name not in tools_data, (
                f"{name} should NOT be in tools_data with responses mode"
            )

    async def test_absent_when_flag_missing(self):
        from gc_core.tools import abuild_tools_data
        from gc_core.tools.subagents.registry import SUBAGENT_CLASSES

        cfg = _base_config(backend_api_mode="chat_completions")
        del cfg["use_local_subagents"]
        tools_data, _, _, _ = await abuild_tools_data(make_settings(cfg))
        subagent_names = {cls.agent_name for cls in SUBAGENT_CLASSES}
        for name in subagent_names:
            assert name not in tools_data

    async def test_specs_have_required_field(self):
        from gc_core.tools import abuild_tools_data
        from gc_core.tools.subagents.registry import SUBAGENT_CLASSES

        cfg = _base_config(
            use_local_subagents=True, backend_api_mode="chat_completions"
        )
        tools_data, _, _, _ = await abuild_tools_data(make_settings(cfg))
        subagent_names = {cls.agent_name for cls in SUBAGENT_CLASSES}
        for name in subagent_names:
            spec = tools_data[name]["spec"]
            params = spec.get("parameters", {})
            assert "required" in params and isinstance(
                params["required"], list
            ), (
                f"{name}: spec.parameters.required must be a list after normalization"
            )

    async def test_subagent_specs_in_tools_spec_list(self):
        from gc_core.tools import abuild_tools_data
        from gc_core.tools.subagents.registry import SUBAGENT_CLASSES

        cfg = _base_config(
            use_local_subagents=True, backend_api_mode="chat_completions"
        )
        _, tools_spec, _, _ = await abuild_tools_data(make_settings(cfg))
        spec_names = {s["name"] for s in tools_spec}
        subagent_names = {cls.agent_name for cls in SUBAGENT_CLASSES}
        assert subagent_names.issubset(spec_names)
