"""Доступность субагента и его права без раннера (§6.5, §12)."""

from gc_core.config_modules.root import AppConfig
from gc_core.plugins.snapshot import PluginSnapshot
from gc_core.settings import LoadedSettings, RuntimeEnv
from gc_core.tools.subagents.custom.file_definitions import (
    SubagentDefinition,
)
from gc_core.tools.subagents.role_info import (
    RoleRights,
    role_availability,
    role_rights,
)

NAME = "subagent_p_r"


def _role(name=NAME, tools=None, mcp_allow=(), path="/p/r.md"):
    return SubagentDefinition(
        name=name,
        description="d",
        role="Р.",
        tools=tools,
        stop_tools=(),
        source_path=path,
        mcp_allow=mcp_allow,
    )


def _settings(*roles, **agent):
    agent = {"use_local_subagents": True, **agent}
    return LoadedSettings(
        settings=AppConfig.model_validate(
            {"agent": agent, "backend": {"api_mode": "chat_completions"}}
        ),
        runtime=RuntimeEnv(plugins=PluginSnapshot(roles=tuple(roles))),
    )


def test_available_role_has_no_reason():
    assert role_availability(_settings(_role()), NAME) is None


def test_subagents_disabled():
    cfg = _settings(_role(), use_local_subagents=False)
    assert "выключены" in role_availability(cfg, NAME)


def test_role_not_in_allowlist():
    cfg = _settings(_role(), use_local_subagents=["subagent_other"])
    assert "use_local_subagents" in role_availability(cfg, NAME)


def test_listed_role_is_available():
    cfg = _settings(_role(), use_local_subagents=[NAME])
    assert role_availability(cfg, NAME) is None


def test_responses_mode_is_unsupported():
    cfg = LoadedSettings(
        settings=AppConfig.model_validate(
            {
                "agent": {"use_local_subagents": True},
                "backend": {"api_mode": "responses"},
            }
        ),
        runtime=RuntimeEnv(plugins=PluginSnapshot(roles=(_role(),))),
    )
    assert "chat_completions" in role_availability(cfg, NAME)


def test_max_count_exceeded():
    cfg = _settings(_role("subagent_a"), _role(), subagents_max_count=1)
    assert role_availability(cfg, "subagent_a") is None
    assert "subagents_max_count" in role_availability(cfg, NAME)


def test_name_taken_by_builtin():
    cfg = _settings(_role("subagent_worker"))
    assert "занято" in role_availability(cfg, "subagent_worker")


def test_name_taken_by_folder_definition(tmp_path):
    (tmp_path / "a.md").write_text(
        f"---\nname: {NAME}\ndescription: d\n---\nР.\n", encoding="utf-8"
    )
    cfg = _settings(_role(), subagents_dir=str(tmp_path))
    assert "занято" in role_availability(cfg, NAME)


def test_all_tools_role():
    rights = role_rights(_settings(), _role(tools=None))
    assert rights == RoleRights(
        edit=True, exec=True, all_tools=True, mcp=False
    )


def test_exec_only_role():
    rights = role_rights(_settings(), _role(tools=("exec_command",)))
    assert rights == RoleRights(
        edit=False, exec=True, all_tools=False, mcp=False
    )


def test_edit_only_role():
    rights = role_rights(_settings(), _role(tools=("fs_write_file",)))
    assert rights == RoleRights(
        edit=True, exec=False, all_tools=False, mcp=False
    )


def test_read_only_role_with_mcp():
    role = _role(tools=("fs_read_file",), mcp_allow=(("p_gh", None),))
    rights = role_rights(_settings(), role)
    assert rights == RoleRights(
        edit=False, exec=False, all_tools=False, mcp=True
    )
