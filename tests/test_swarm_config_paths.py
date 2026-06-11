from pathlib import Path

import pytest

from config_modules import env_vars
from config_modules.env_vars import EnvVars, project_slug
from swarm.config import build_swarm_config


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.platform", "linux")
    fake = EnvVars(gc_home=tmp_path)
    monkeypatch.setattr(env_vars, "env", fake)
    monkeypatch.chdir(tmp_path)
    return fake


def test_swarm_threads_default_under_state_projects_slug(isolated_env, tmp_path):
    cfg = build_swarm_config({})
    threads_dir = Path(cfg["threads_dir"])
    assert threads_dir == tmp_path / "projects" / project_slug() / "swarm" / "threads"


def test_swarm_sandbox_default_under_state_projects_slug(isolated_env, tmp_path):
    cfg = build_swarm_config({})
    sandbox_dir = Path(cfg["sandbox_root_dir"])
    assert sandbox_dir == tmp_path / "projects" / project_slug() / "swarm" / "sandboxes"


def test_swarm_explicit_threads_dir_used_as_is(isolated_env, tmp_path):
    explicit = tmp_path / "custom_threads"
    cfg = build_swarm_config({"swarm": {"threads_dir": str(explicit)}})
    assert Path(cfg["threads_dir"]) == explicit


def test_swarm_wrapper_and_agentmd_paths_unchanged(isolated_env, tmp_path):
    """Пакетные ресурсы остаются с PROJECT_ROOT-дефолтами."""
    cfg = build_swarm_config({})
    assert tmp_path not in Path(cfg["wrapper_path"]).parents
    assert tmp_path not in Path(cfg["agentmd_path"]).parents
    assert tmp_path not in Path(cfg["skills_dir"]).parents


# ---------------------------------------------------------------------------
# agent_overrides propagation (унификация webhook + swarm)
# ---------------------------------------------------------------------------


def test_swarm_propagates_agent_overrides_from_server_cfg(isolated_env):
    """server_cfg.agent_overrides должен попасть в swarm_cfg как есть.

    Materializer потом авто-обернёт flat-формат в {agent: ...}.
    """
    server_cfg = {
        "agent_overrides": {
            "stop_tools": ["write_to_file"],
            "max_steps": 100,
        },
    }
    cfg = build_swarm_config(server_cfg)
    assert cfg["agent_overrides"] == {
        "stop_tools": ["write_to_file"],
        "max_steps": 100,
    }


def test_swarm_propagates_agent_overrides_raw_form(isolated_env):
    """Raw-формат overrides (с секциями agent: / backend: ) пробрасывается без изменений."""
    server_cfg = {
        "agent_overrides": {
            "agent": {"max_steps": 999},
            "backend": {"api_mode": "responses"},
        },
    }
    cfg = build_swarm_config(server_cfg)
    assert cfg["agent_overrides"]["agent"]["max_steps"] == 999
    assert cfg["agent_overrides"]["backend"]["api_mode"] == "responses"


def test_swarm_agent_overrides_empty_when_missing(isolated_env):
    """Если в server_cfg нет agent_overrides — в swarm_cfg ключ есть, но пустой."""
    cfg = build_swarm_config({})
    assert cfg["agent_overrides"] == {}


def test_swarm_agent_overrides_invalid_type_falls_back_to_empty(isolated_env):
    """agent_overrides не-dict (list, str) — fallback на пустой dict, без падения."""
    cfg = build_swarm_config({"agent_overrides": ["not", "a", "dict"]})
    assert cfg["agent_overrides"] == {}

    cfg = build_swarm_config({"agent_overrides": "stop_tools"})
    assert cfg["agent_overrides"] == {}


def test_swarm_agent_overrides_is_a_copy_not_reference(isolated_env):
    """build_swarm_config должен сделать копию, чтобы мутации swarm_cfg
    не затрагивали оригинал в server_cfg."""
    original = {"stop_tools": ["write_to_file"]}
    server_cfg = {"agent_overrides": original}
    cfg = build_swarm_config(server_cfg)
    cfg["agent_overrides"]["max_steps"] = 100
    assert "max_steps" not in original
