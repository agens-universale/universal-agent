"""Tests for config-driven injection of synthetic tool schema fields."""

import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import tools as tools_pkg
from tools import apply_tool, build_tools_data, WrongFunctionArgumentsException
from tools.inject_tool_fields import (
    apply_injected_fields_to_spec,
    resolve_inject_tool_fields,
    strip_injected_tool_args,
)

_REASON = "reason"
_REASON_DESC = (
    "Brief rationale (1–2 sentences): goal, why now, expected outcome."
)


def _reason_fields(**extra: str) -> dict[str, str]:
    fields = {_REASON: _REASON_DESC}
    fields.update(extra)
    return fields


@pytest.fixture(autouse=True)
def _ensure_real_tools_module():
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
        "mcp_servers": {},
        "stop_tools": [],
    }
    cfg.update(overrides)
    return cfg


def _write_to_file_spec():
    return {
        "type": "function",
        "name": "fs_write_file",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    }


class TestResolveInjectToolFields:
    def test_missing_key_no_inject(self):
        assert resolve_inject_tool_fields({}) == {}

    def test_empty_dict_disables_inject(self):
        assert resolve_inject_tool_fields({"inject_tool_fields": {}}) == {}

    def test_custom_field_from_config(self):
        cfg = {"inject_tool_fields": {"why": "Explain why."}}
        assert resolve_inject_tool_fields(cfg) == {"why": "Explain why."}


class TestInjectPropertyIntoSpec:
    def test_injects_reason_first(self):
        spec = _write_to_file_spec()
        injected = apply_injected_fields_to_spec(spec, {_REASON: _REASON_DESC})
        props = spec["parameters"]["properties"]
        assert injected == [_REASON]
        assert list(props.keys())[0] == _REASON
        assert props[_REASON]["description"] == _REASON_DESC
        assert spec["parameters"]["required"][0] == _REASON

    def test_skips_when_field_exists(self):
        spec = {
            "name": "native_reason_tool",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "native"},
                    "task": {"type": "string"},
                },
                "required": ["reason", "task"],
            },
        }
        before = json.dumps(spec)
        assert apply_injected_fields_to_spec(spec, _reason_fields()) == []
        assert json.dumps(spec) == before

    def test_empty_properties_gets_only_reason(self):
        spec = {"name": "empty_tool", "parameters": {"type": "object", "properties": {}}}
        assert apply_injected_fields_to_spec(spec, _reason_fields()) == [_REASON]

    def test_missing_parameters_creates_object(self):
        spec = {"name": "no_params"}
        assert apply_injected_fields_to_spec(spec, _reason_fields()) == [_REASON]
        assert spec["parameters"]["required"] == [_REASON]

    def test_multiple_fields_from_config(self):
        spec = _write_to_file_spec()
        fields = {"audit": "Audit note.", "reason": _REASON_DESC}
        injected = apply_injected_fields_to_spec(spec, fields)
        assert injected == ["audit", _REASON]
        assert list(spec["parameters"]["properties"].keys())[:2] == ["reason", "audit"]


class TestStripInjectedToolArgs:
    def test_strips_only_listed_fields(self):
        args = {_REASON: "because", "path": "/tmp/x"}
        assert strip_injected_tool_args(args, injected_fields=[_REASON]) == {
            "path": "/tmp/x"
        }


class TestBuildToolsData:
    def test_disabled_leaves_spec_unchanged(self):
        tools_data, _, _, _ = build_tools_data(_base_config(inject_tool_fields={}))
        entry = tools_data["fs_write_file"]
        assert _REASON not in entry["spec"]["parameters"]["properties"]
        assert entry.get("injected_fields", []) == []

    def test_enabled_injects_from_config_dict(self):
        tools_data, _, _, _ = build_tools_data(_base_config(inject_tool_fields=_reason_fields()))
        entry = tools_data["fs_write_file"]
        props = entry["spec"]["parameters"]["properties"]
        assert list(props.keys())[0] == _REASON
        assert entry["injected_fields"] == [_REASON]

    def test_subagent_with_native_reason_not_injected(self):
        cfg = _base_config(
            inject_tool_fields=_reason_fields(),
            use_local_subagents=True,
            backend_api_mode="chat_completions",
        )
        tools_data, _, _, _ = build_tools_data(cfg)
        assert tools_data["subagent_worker"]["injected_fields"] == []

    def test_strict_plus_inject_makes_all_required(self):
        tools_data, _, _, _ = build_tools_data(
            _base_config(
                inject_tool_fields=_reason_fields(),
                openai_strict_tools_schema=True,
            )
        )
        params = tools_data["fs_write_file"]["spec"]["parameters"]
        assert set(params["required"]) == set(params["properties"].keys())


class TestApplyTool:
    def _make_ctx(self, tools_data):
        return SimpleNamespace(
            config_dict=_base_config(inject_tool_fields=_reason_fields()),
            tools_data=tools_data,
            logger=None,
        )

    def test_strips_synthetic_reason_before_call(self):
        called = {}

        class FakeTool:
            def __init__(self, **kw):
                pass

            def __call__(self, **kw):
                called["kwargs"] = kw
                return "ok"

        spec = _write_to_file_spec()
        apply_injected_fields_to_spec(spec, _reason_fields())
        tools_data = {
            "fs_write_file": {
                "spec": spec,
                "description": "",
                "tool_fabric": FakeTool,
                "injected_fields": [_REASON],
            }
        }

        apply_tool(
            tool_name="fs_write_file",
            arguments=json.dumps(
                {
                    _REASON: "need to save output",
                    "path": "/tmp/out.txt",
                    "content": "hello",
                }
            ),
            ctx=self._make_ctx(tools_data),
        )
        assert _REASON not in called["kwargs"]

    def test_native_reason_reaches_call(self):
        received = {}

        class NativeReasonTool:
            def __init__(self, **kw):
                pass

            def __call__(self, reason="", task=""):
                received["args"] = {"reason": reason, "task": task}
                return "ok"

        tools_data = {
            "native_reason_tool": {
                "spec": {
                    "name": "native_reason_tool",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "reason": {"type": "string"},
                            "task": {"type": "string"},
                        },
                        "required": ["reason", "task"],
                    },
                },
                "description": "",
                "tool_fabric": NativeReasonTool,
                "injected_fields": [],
            }
        }
        apply_tool(
            tool_name="native_reason_tool",
            arguments=json.dumps({"reason": "native", "task": "work"}),
            ctx=self._make_ctx(tools_data),
        )
        assert received["args"] == {"reason": "native", "task": "work"}

    def test_missing_reason_is_accepted(self):
        """Omitted injected reason no longer rejects the call (it is semantically optional)."""
        tools_data, _, _, _ = build_tools_data(
            _base_config(inject_tool_fields=_reason_fields())
        )

        received = {}

        class FakeTool:
            def __init__(self, **kw):
                pass

            def __call__(self, **kwargs):
                received["args"] = kwargs
                return "ok"

        tools_data["fs_write_file"]["tool_fabric"] = FakeTool

        apply_tool(
            tool_name="fs_write_file",
            arguments=json.dumps({"path": "/tmp/x", "content": "y"}),
            ctx=self._make_ctx(tools_data),
        )
        assert received["args"] == {"path": "/tmp/x", "content": "y"}

    def test_mcp_wrapper_does_not_receive_synthetic_reason(self):
        from tools.connect_mcp.agent_tools import MCPToolWrapper

        spec = _write_to_file_spec()
        apply_injected_fields_to_spec(spec, _reason_fields())
        wrapper = MCPToolWrapper(
            full_tool_name="mcp.test/tool",
            servers_cfg={},
            tools_registry={},
            timeout=1.0,
            default_use_retry=False,
        )
        tools_data = {
            "mcp_test_tool": {
                "spec": spec,
                "description": "",
                "tool_fabric": lambda **kw: wrapper,
                "injected_fields": [_REASON],
            }
        }
        with patch("tools.connect_mcp.agent_tools.call_mcp_tool") as mock_call:
            mock_call.return_value = "mcp ok"
            apply_tool(
                tool_name="mcp_test_tool",
                arguments=json.dumps(
                    {
                        _REASON: "fetch data",
                        "path": "/tmp/x",
                        "content": "data",
                    }
                ),
                ctx=self._make_ctx(tools_data),
            )
            assert _REASON not in mock_call.call_args.kwargs["arguments"]

    def test_subagent_orchestrator_strips_synthetic_reason(self):
        spec = {
            "type": "function",
            "name": "subagent_stub",
            "parameters": {
                "type": "object",
                "properties": {"task": {"type": "string"}},
                "required": ["task"],
            },
        }
        apply_injected_fields_to_spec(spec, _reason_fields())
        ctx = SimpleNamespace(
            config_dict=_base_config(
                inject_tool_fields=_reason_fields(),
                use_local_subagents=True,
                backend_api_mode="chat_completions",
            ),
            tools_data={
                "subagent_stub": {
                    "spec": spec,
                    "description": "",
                    "tool_fabric": None,
                    "injected_fields": [_REASON],
                }
            },
            logger=None,
        )

        run_agent_func = MagicMock()
        with patch.object(tools_pkg, "is_subagent_tool", return_value=True):
            with patch("tools.subagents.runner.SubagentRunner") as MockRunner:
                instance = MockRunner.return_value
                instance.run.return_value = "done"
                apply_tool(
                    tool_name="subagent_stub",
                    arguments=json.dumps(
                        {_REASON: "delegate work", "task": "do something"}
                    ),
                    ctx=ctx,
                    state=MagicMock(),
                    run_agent_func=run_agent_func,
                )
                instance.run.assert_called_once_with(
                    "subagent_stub",
                    {"task": "do something"},
                    run_agent_func,
                    parent_call_id=None,
                )


class TestSubagentChildBuild:
    def test_child_config_inherits_decoration(self):
        parent = _base_config(inject_tool_fields=_reason_fields())
        child = {**parent, "use_local_subagents": False}
        tools_data, _, _, _ = build_tools_data(child)
        assert tools_data["fs_read_file"]["injected_fields"] == [_REASON]


class TestInjectDoesNotPolluteClassSchema:
    def test_second_build_still_tracks_injected_fields(self):
        """Regression: inject must not mutate shared class-level parameters.

        Without a deepcopy in the registry/tool_description, the first
        build_tools_data() leaves ``reason`` on the class schema; the second
        build then skips inject (field already present) and leaves
        injected_fields empty — apply_tool forwards reason → TypeError.
        """
        cfg = _base_config(inject_tool_fields=_reason_fields())
        first, _, _, _ = build_tools_data(cfg)
        second, _, _, _ = build_tools_data(cfg)

        for name in ("fs_find_in_files", "fs_write_file", "fs_read_file"):
            assert first[name]["injected_fields"] == [_REASON]
            assert second[name]["injected_fields"] == [_REASON]
            # class-level / fresh tool_description stays clean of inject
            fabric = second[name]["tool_fabric"]
            fresh = fabric.tool_description()
            assert _REASON not in (fresh.get("parameters") or {}).get("properties", {})

    def test_second_build_strips_reason_on_apply(self):
        cfg = _base_config(inject_tool_fields=_reason_fields())
        build_tools_data(cfg)  # first build (pollution attempt)
        tools_data, _, _, _ = build_tools_data(cfg)  # second — must still strip

        received = {}

        class FakeTool:
            def __init__(self, **kw):
                pass

            def __call__(self, **kwargs):
                received["args"] = kwargs
                return "ok"

        tools_data["fs_find_in_files"]["tool_fabric"] = FakeTool
        apply_tool(
            tool_name="fs_find_in_files",
            arguments=json.dumps(
                {
                    _REASON: "test",
                    "path": "/tmp/x",
                    "pattern": "def test_",
                    "query_mode": "python-re",
                    "limit": 5,
                }
            ),
            ctx=SimpleNamespace(
                config_dict=cfg,
                tools_data=tools_data,
                logger=None,
            ),
        )
        assert _REASON not in received["args"]
        assert received["args"]["path"] == "/tmp/x"
        assert received["args"]["pattern"] == "def test_"
