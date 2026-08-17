from pathlib import Path

import pytest

from gc_core.config_modules import env_vars
from gc_core.config_modules.common import apply_swarm_env_overrides
from gc_core.config_modules.env_vars import EnvVars


def test_swarm_threads_dir_overrides_threads_data_path(monkeypatch, tmp_path):
    custom = tmp_path / "swarm_override"
    monkeypatch.setattr(env_vars, "env", EnvVars(swarm_threads_dir=custom))
    config_dict = {"threads_data_path": "/should/be/overridden"}
    apply_swarm_env_overrides(config_dict, str(tmp_path))
    assert Path(config_dict["threads_data_path"]) == custom


def test_swarm_agentmd_path_override(monkeypatch, tmp_path):
    custom = tmp_path / "agent.md"
    monkeypatch.setattr(env_vars, "env", EnvVars(swarm_agentmd_path=custom))
    config_dict = {}
    apply_swarm_env_overrides(config_dict, str(tmp_path))
    assert Path(config_dict["agent_md_path"]) == custom


def test_swarm_no_override_when_env_unset(monkeypatch, tmp_path):
    monkeypatch.setattr(env_vars, "env", EnvVars())
    config_dict = {"threads_data_path": "/original"}
    apply_swarm_env_overrides(config_dict, str(tmp_path))
    assert config_dict["threads_data_path"] == "/original"
