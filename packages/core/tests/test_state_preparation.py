"""Preparation owns external reads; State assembly and replay use only values."""

import builtins
import json
import os
import time
from dataclasses import asdict
from pathlib import Path

import pytest
from gc_core import state_preparation, states
from gc_core.execution.state import SharedStateData
from gc_core.instructions.loader import AGENTSMD_OVERRIDE_IGNORED_WARNING
from gc_core.state_preparation import PreparedState, prepare_state
from gc_core.states import State


def _arguments(**overrides):
    return {
        "verbose": False,
        "model": "m",
        "tools_spec": [],
        "tools_descr": [],
        "skills_list": [],
        "max_steps": 2,
        "system_prompt_id": "system",
        "use_server_subagents": False,
        **overrides,
    }


def _prepared():
    return PreparedState(
        state_id="prepared-id",
        project_data={"currentDate": "2026-10-01", "projectDir": "/workspace"},
        instructions_text="captured instructions",
        instruction_warnings=["captured warning"],
        long_memory_status="captured memory status",
    )


class ForbiddenPreparationIO(BaseException):
    """A swallowed OSError must not let the no-I/O sentinel pass."""


def _forbid_io(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise ForbiddenPreparationIO(
            "external operation during State assembly"
        )

    for module, name in (
        (builtins, "open"),
        (Path, "open"),
        (Path, "read_text"),
        (os, "stat"),
        (os, "getcwd"),
        (time, "time"),
        (time, "monotonic"),
        (states, "uuid4"),
        (state_preparation, "uuid4"),
        (states, "aload_state_metadata"),
        (states, "build_execution_workspace"),
    ):
        monkeypatch.setattr(module, name, forbidden)


def test_prepared_assembly_refresh_and_snapshot_do_not_perform_io(monkeypatch):
    prepared = _prepared()
    original = asdict(prepared)
    shared = SharedStateData(confirm_rules={"write": True})
    with monkeypatch.context() as sentinel:
        _forbid_io(sentinel)
        state = State.from_prepared(
            prepared, **_arguments(shared=shared, specific_role="reviewer")
        )
        metadata = state.set_call_metadata()
        snapshot = json.loads(json.dumps(state.to_execution_snapshot()))
        restored = State.from_execution_snapshot(snapshot)
        restored_metadata = restored.set_call_metadata()
    assert state.shared is shared
    assert state.state_id == "prepared-id"
    assert state.instruction_warnings == ["captured warning"]
    assert metadata == restored_metadata
    assert metadata["specificRole"] == "reviewer"
    assert (
        "captured instructions"
        in metadata["promptVariables"]["userInstructions"]
    )
    assert (
        metadata["promptVariables"]["longMemoryStatus"]
        == "captured memory status"
    )
    assert snapshot["data"]["prepared_metadata"] is True
    assert asdict(prepared) == original


def test_limited_project_metadata_keeps_only_date_before_enrichment(
    monkeypatch,
):
    with monkeypatch.context() as sentinel:
        _forbid_io(sentinel)
        state = State.from_prepared(
            _prepared(), **_arguments(limit_project_metadata=True)
        )
    variables = state.call_metadata["promptVariables"]
    assert variables["currentDate"] == "2026-10-01"
    assert "projectDir" not in variables
    assert "longMemoryStatus" in variables
    assert "userInstructions" in variables


def test_prepared_assembly_rejects_unresolved_backend_without_io(monkeypatch):
    with monkeypatch.context() as sentinel:
        _forbid_io(sentinel)
        with pytest.raises(ValueError, match="already built workspace"):
            State.from_prepared(_prepared(), **_arguments(os_backend=object()))


@pytest.mark.parametrize(
    "is_tui,override,use_l0,expected,excluded",
    [
        (False, False, False, [], ["GLOBAL", "PROJECT"]),
        (True, False, False, ["GLOBAL", "PROJECT"], []),
        (True, True, False, ["PROJECT"], ["GLOBAL"]),
        (True, True, True, ["ROLE"], ["GLOBAL", "PROJECT"]),
    ],
)
async def test_preparation_preserves_native_instruction_precedence_and_status(
    tmp_path, monkeypatch, is_tui, override, use_l0, expected, excluded
):
    from gc_core.config_modules import env_vars
    from gc_core.utils import project_data

    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (home / "AGENTS.md").write_text("GLOBAL", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("PROJECT", encoding="utf-8")
    role = tmp_path / "role.md"
    role.write_text("ROLE", encoding="utf-8")
    monkeypatch.setattr(env_vars.env, "gc_home", home)
    monkeypatch.setattr(env_vars.env, "gc_config_dir", None)
    monkeypatch.setattr(states, "uuid4", lambda: "prepared-id")
    monkeypatch.setattr(state_preparation, "uuid4", lambda: "prepared-id")
    project_values = {"currentDate": "2026-10-01", "projectDir": str(tmp_path)}

    async def data(*_args, **_kwargs):
        return dict(project_values)

    monkeypatch.setattr(project_data, "get_project_data", data)
    kwargs = _arguments(
        is_tui=is_tui,
        agentsmd_override=override,
        agent_md_path=str(role) if use_l0 else None,
    )
    direct = State(initial_project_data=project_values, **kwargs)
    from gc_testkit.settings import make_settings

    # Роль запуска (L0) читает хранилище агента по настройкам прогона.
    prepared = await prepare_state(
        workspace_config=make_settings(
            {"paths.agent_md_path": kwargs["agent_md_path"] or ""}
        ),
        is_tui=is_tui,
        agentsmd_override=override,
    )
    assembled = State.from_prepared(prepared, **kwargs)
    assert assembled.call_metadata == direct.call_metadata
    assert assembled.instruction_warnings == direct.instruction_warnings
    text = assembled.call_metadata["promptVariables"].get(
        "userInstructions", ""
    )
    for word in expected:
        assert word in text
    for word in excluded:
        assert word not in text
    if use_l0 and override:
        assert (
            AGENTSMD_OVERRIDE_IGNORED_WARNING in prepared.instruction_warnings
        )
    # The payload is independent of later file changes; only a new preparation
    # should capture them. Existing direct callers retain explicit local refresh.
    if use_l0:
        role.write_text("NEW ROLE", encoding="utf-8")
        assert (
            "NEW ROLE"
            not in assembled.set_call_metadata()["promptVariables"][
                "userInstructions"
            ]
        )
        assert (
            "NEW ROLE"
            in direct.set_call_metadata()["promptVariables"][
                "userInstructions"
            ]
        )


async def test_local_factory_uses_prepared_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = await State.acreate(**_arguments())
    assert state.prepared_metadata is True
    assert state.initial_project_data["projectDir"] == str(tmp_path)
    with monkeypatch.context() as sentinel:
        _forbid_io(sentinel)
        metadata = state.set_call_metadata()
    assert metadata == state.call_metadata
