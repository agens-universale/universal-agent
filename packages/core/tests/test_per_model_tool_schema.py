"""Проекция схемы тулов на алиас в момент chat_completions.

Канон в state.tools не зависит от флагов. На провод уходит копия под
резолвнутую модель этого запроса. Спека:
docs_md/actual/2026-10-05-per-model-tool-schema-SPEC.ru.md
"""

import copy
import json
from types import SimpleNamespace

from gc_core.config_modules.backend import BackendConfig
from gc_core.config_modules.model_registry import parse_model_backend_text
from gc_core.model_router import ResolvedModel
from gc_core.tools import _finish_tools_data
from gc_core.tools.arguments_validation import (
    parse_and_validate_tool_arguments,
)
from gc_core.execution.tool_schema import (
    OPTIONAL_EMPTY_AS_OMIT_HINT,
    OPTIONAL_OMIT_HINT,
)
from gc_core.utils.connect_backend import (
    create_call_data_completions,
    create_call_data_responses,
)
from gc_core.utils.llm_chat_client import _build_chat_completion_payload
from gc_testkit.settings import make_settings


def _canon_tool() -> dict:
    return {
        "type": "function",
        "name": "note_tool",
        "description": "Write a note.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Where to write"},
                "note": {"type": "string", "description": "Optional remark"},
                "nested": {
                    "type": "object",
                    "description": "Inner",
                    "properties": {
                        "a": {"type": "string", "description": "A"},
                        "b": {"type": "string", "description": "B"},
                    },
                    "required": ["a"],
                },
            },
            "required": ["path"],
        },
    }


def _resolved(**overrides) -> ResolvedModel:
    base = dict(
        alias="flash",
        model="flash-model",
        url="http://alias.local/v1/chat/completions",
        authorization=None,
        backend_overrides={},
        tariff=None,
        source="test",
    )
    base.update(overrides)
    return ResolvedModel(**base)


def _state(tools) -> SimpleNamespace:
    return SimpleNamespace(
        messages=[],
        tools=tools,
        default_model="legacy",
        call_metadata={},
        previous_response_id=None,
    )


def _wire_function(data: dict) -> dict:
    return data["tools"][0]["function"]


def _settings(flags: dict):
    """Флаги схемы общего backend (``openai_strict_tools_schema``…)."""
    return make_settings(
        {f"backend.{key}": value for key, value in flags.items()}
    )


def _send(tools, *, config=None, resolved=None):
    return create_call_data_completions(
        [{"role": "user", "content": "hi"}],
        _state(tools),
        settings=_settings(config or {}),
        resolved=resolved,
    )


def test_shipping_defaults_stay_wide():
    assert BackendConfig().openai_strict_tools_schema is False
    assert BackendConfig().send_tool_strict is True


def test_alias_backend_accepts_tool_schema_flags():
    overlay = parse_model_backend_text(
        "openai_strict_tools_schema: true\nsend_tool_strict: false\n"
    )
    assert overlay == {
        "openai_strict_tools_schema": True,
        "send_tool_strict": False,
    }


def test_finish_tools_data_keeps_canonical_schema_regardless_of_flags():
    def _built(flags: dict) -> dict:
        spec = _canon_tool()
        tools_data, specs, *_ = _finish_tools_data(
            {
                "note_tool": {
                    "spec": spec,
                    "description": "Write a note.",
                }
            },
            {},
            {},
            _settings(flags),
            logger=None,
        )
        return tools_data["note_tool"]["spec"], specs[0]

    loose, loose_spec = _built(
        {"openai_strict_tools_schema": False, "send_tool_strict": True}
    )
    strict, strict_spec = _built(
        {"openai_strict_tools_schema": True, "send_tool_strict": False}
    )
    assert loose["parameters"]["required"] == ["path"]
    assert loose["parameters"]["properties"]["nested"]["required"] == ["a"]
    assert loose.get("strict") is True
    assert (
        OPTIONAL_OMIT_HINT
        not in loose["parameters"]["properties"]["note"]["description"]
    )
    assert (
        OPTIONAL_EMPTY_AS_OMIT_HINT
        not in loose["parameters"]["properties"]["note"]["description"]
    )
    assert loose == strict
    assert loose_spec == strict_spec


def test_alias_without_keys_matches_global_projection():
    tools = [_canon_tool()]
    global_cfg = {
        "openai_strict_tools_schema": True,
        "send_tool_strict": False,
    }
    from_global = _send(copy.deepcopy(tools), config=global_cfg)
    from_alias = _send(
        copy.deepcopy(tools),
        config=global_cfg,
        resolved=_resolved(backend_overrides={"use_max_tokens_param": True}),
    )
    assert json.dumps(from_global["tools"], sort_keys=True) == json.dumps(
        from_alias["tools"], sort_keys=True
    )
    fn = _wire_function(from_alias)
    assert "strict" not in fn
    assert set(fn["parameters"]["required"]) == {"path", "note", "nested"}


def test_alias_send_tool_strict_false_drops_strict_only():
    data = _send(
        [_canon_tool()],
        config={"openai_strict_tools_schema": False, "send_tool_strict": True},
        resolved=_resolved(backend_overrides={"send_tool_strict": False}),
    )
    fn = _wire_function(data)
    assert "strict" not in fn
    assert fn["parameters"]["required"] == ["path"]
    assert (
        OPTIONAL_OMIT_HINT
        in fn["parameters"]["properties"]["note"]["description"]
    )


def test_alias_openai_strict_expands_required_and_keeps_strict():
    data = _send(
        [_canon_tool()],
        config={"openai_strict_tools_schema": False, "send_tool_strict": True},
        resolved=_resolved(
            backend_overrides={"openai_strict_tools_schema": True}
        ),
    )
    fn = _wire_function(data)
    assert fn["strict"] is True
    assert fn["parameters"]["required"] == ["path", "note", "nested"]
    assert fn["parameters"]["additionalProperties"] is False
    assert fn["parameters"]["properties"]["nested"]["required"] == ["a", "b"]
    assert (
        fn["parameters"]["properties"]["nested"]["additionalProperties"]
        is False
    )
    note = fn["parameters"]["properties"]["note"]["description"]
    assert OPTIONAL_EMPTY_AS_OMIT_HINT in note
    assert OPTIONAL_OMIT_HINT not in note


def test_same_model_and_flags_project_byte_identically():
    tools = [_canon_tool()]
    resolved = _resolved(
        backend_overrides={
            "openai_strict_tools_schema": True,
            "send_tool_strict": False,
        }
    )
    first = _send(copy.deepcopy(tools), resolved=resolved)
    second = _send(copy.deepcopy(tools), resolved=resolved)
    assert json.dumps(first["tools"], ensure_ascii=False) == json.dumps(
        second["tools"], ensure_ascii=False
    )


def test_two_aliases_project_per_call_without_rewriting_state_tools():
    tools = [_canon_tool()]
    state = _state(tools)
    before = copy.deepcopy(tools)
    loose = _resolved(alias="loose", model="loose-model")
    strict = _resolved(
        alias="strict",
        model="strict-model",
        backend_overrides={
            "openai_strict_tools_schema": True,
            "send_tool_strict": False,
        },
    )
    first = create_call_data_completions(
        [{"role": "user", "content": "hi"}],
        state,
        settings=_settings({"send_tool_strict": True}),
        resolved=loose,
    )
    second = create_call_data_completions(
        [{"role": "user", "content": "hi"}],
        state,
        settings=_settings({"send_tool_strict": True}),
        resolved=strict,
    )
    assert state.tools == before
    assert _wire_function(first)["parameters"]["required"] == ["path"]
    assert "strict" in _wire_function(first)
    assert set(_wire_function(second)["parameters"]["required"]) == {
        "path",
        "note",
        "nested",
    }
    assert "strict" not in _wire_function(second)


def test_local_validation_reads_canon_not_wire():
    canon = _canon_tool()
    wire = _send(
        [copy.deepcopy(canon)],
        resolved=_resolved(
            backend_overrides={"openai_strict_tools_schema": True}
        ),
    )
    assert "note" in _wire_function(wire)["parameters"]["required"]
    omitted = parse_and_validate_tool_arguments(
        tool_name="note_tool",
        arguments={"path": "/tmp/a"},
        tool_spec=canon,
        allow_empty_for_empty_schema=False,
    )
    assert omitted == {"path": "/tmp/a"}
    empty = parse_and_validate_tool_arguments(
        tool_name="note_tool",
        arguments={"path": "/tmp/a", "note": ""},
        tool_spec=canon,
        allow_empty_for_empty_schema=False,
    )
    assert "note" not in empty
    assert empty["path"] == "/tmp/a"


def test_subagent_projects_its_own_model_not_the_parent():
    tools = [_canon_tool()]
    state = _state(tools)
    before = copy.deepcopy(tools)
    parent = _resolved(alias="parent", model="parent-model")
    child = _resolved(
        alias="child",
        model="child-model",
        backend_overrides={"openai_strict_tools_schema": True},
    )
    parent_data = create_call_data_completions(
        [{"role": "user", "content": "hi"}],
        state,
        resolved=parent,
    )
    child_data = create_call_data_completions(
        [{"role": "user", "content": "hi"}],
        state,
        resolved=child,
    )
    assert state.tools == before
    assert _wire_function(parent_data)["parameters"]["required"] == ["path"]
    assert _wire_function(child_data)["parameters"]["required"] == [
        "path",
        "note",
        "nested",
    ]


def test_service_call_without_tools_does_not_touch_schema():
    tools = [_canon_tool()]
    state = _state(tools)
    before = copy.deepcopy(tools)
    payload = _build_chat_completion_payload(
        ctx=_settings(
            {
                "openai_strict_tools_schema": True,
                "send_tool_strict": False,
            }
        ),
        state=state,
        messages=[{"role": "user", "content": "sum"}],
        max_tokens=32,
        resolved=_resolved(
            backend_overrides={"openai_strict_tools_schema": True}
        ),
    )
    assert "tools" not in payload
    assert state.tools == before
    assert state.tools[0]["parameters"]["required"] == ["path"]


def test_responses_projects_global_flags_and_ignores_alias():
    tools = [_canon_tool()]
    state = _state(tools)
    before = copy.deepcopy(tools)
    alias = _resolved(
        backend_overrides={
            "openai_strict_tools_schema": True,
            "send_tool_strict": False,
        }
    )
    data = create_call_data_responses(
        [{"role": "user", "content": "hi"}],
        state,
        resolved=alias,
        settings=_settings(
            {
                "openai_strict_tools_schema": False,
                "send_tool_strict": True,
            }
        ),
    )
    assert state.tools == before
    assert data["tools"][0]["parameters"]["required"] == ["path"]
    assert data["tools"][0]["strict"] is True
    assert (
        OPTIONAL_OMIT_HINT
        in data["tools"][0]["parameters"]["properties"]["note"]["description"]
    )

    strict_global = create_call_data_responses(
        [{"role": "user", "content": "hi"}],
        state,
        resolved=_resolved(),
        settings=_settings(
            {
                "openai_strict_tools_schema": True,
                "send_tool_strict": False,
            }
        ),
    )
    projected = strict_global["tools"][0]
    assert "strict" not in projected
    assert projected["parameters"]["required"] == ["path", "note", "nested"]
    assert (
        OPTIONAL_EMPTY_AS_OMIT_HINT
        in projected["parameters"]["properties"]["note"]["description"]
    )


def test_responses_explicit_empty_tools_not_revived_by_projection():
    tool = _canon_tool()
    state = SimpleNamespace(
        default_model="responses-model",
        tools=[tool],
        call_metadata={},
        previous_response_id=None,
    )
    data = create_call_data_responses(
        [],
        state,
        tools=[],
        settings=_settings({"openai_strict_tools_schema": True}),
    )
    assert data["tools"] == []
    assert state.tools == [tool]
