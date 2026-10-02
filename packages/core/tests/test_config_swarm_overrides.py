from pathlib import Path

from gc_core.config import flat_config, load_settings
from gc_core.config_modules import env_vars
from gc_core.config_modules.common import swarm_env_updates
from gc_core.config_modules.env_vars import EnvVars


def test_swarm_threads_dir_overrides_threads_data_path(monkeypatch, tmp_path):
    custom = tmp_path / "swarm_override"
    monkeypatch.setattr(env_vars, "env", EnvVars(swarm_threads_dir=custom))
    updates = swarm_env_updates(str(tmp_path))
    assert Path(updates["paths.threads_data_path"]) == custom


def test_swarm_agentmd_path_override(monkeypatch, tmp_path):
    custom = tmp_path / "agent.md"
    monkeypatch.setattr(env_vars, "env", EnvVars(swarm_agentmd_path=custom))
    assert (
        Path(swarm_env_updates(str(tmp_path))["paths.agent_md_path"]) == custom
    )


def test_swarm_relative_path_resolves_from_config_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(
        env_vars, "env", EnvVars(swarm_skills_dir=Path("skills"))
    )
    updates = swarm_env_updates(str(tmp_path))
    assert updates["paths.skills_data_path"] == str(tmp_path / "skills")


def test_swarm_no_override_when_env_unset(monkeypatch, tmp_path):
    monkeypatch.setattr(env_vars, "env", EnvVars())
    assert swarm_env_updates(str(tmp_path)) == {}


def test_load_settings_applies_swarm_env_as_its_own_layer(
    monkeypatch, tmp_path
):
    # Переопределение живёт в настройках, а не только в плоском виде: его
    # видит каждый, кто читает ctx.settings, и explain называет источник.
    conf = tmp_path / "config.yml"
    conf.write_text(
        f"paths:\n  threads_data_path: {tmp_path / 'from_file'}\n",
        encoding="utf-8",
    )
    custom = tmp_path / "swarm_threads"
    monkeypatch.setattr(env_vars, "env", EnvVars(swarm_threads_dir=custom))
    loaded = load_settings(str(conf))
    assert Path(loaded.settings.paths.threads_data_path) == custom
    assert flat_config(loaded)["threads_data_path"] == str(custom)
    assert loaded.provenance.source("paths.threads_data_path").layer == "env"
