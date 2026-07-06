import config
from config_modules import env_vars


def test_bootstrap_creates_user_config(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "gc"
    monkeypatch.setattr(env_vars, "gc_paths", lambda: {"config": cfg_dir,
                                                       "state": tmp_path / "state",
                                                       "data": tmp_path / "data",
                                                       "cache": tmp_path / "cache"})
    monkeypatch.setattr(env_vars, "project_slug", lambda: "testproj")
    dest = cfg_dir / "config.yml"
    assert not dest.exists()
    cfg = config.load_config()          # no --config, no ~/.gc/config.yml
    assert dest.is_file()               # bootstrap создал
    assert cfg["model"] == "gigacode_agent_v1_rc"


def test_bootstrap_does_not_clobber_existing(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "gc"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.yml").write_text("agent:\n  model: custom-x\n", encoding="utf-8")
    monkeypatch.setattr(env_vars, "gc_paths", lambda: {"config": cfg_dir,
                                                       "state": tmp_path / "state",
                                                       "data": tmp_path / "data",
                                                       "cache": tmp_path / "cache"})
    monkeypatch.setattr(env_vars, "project_slug", lambda: "testproj")
    cfg = config.load_config()
    assert cfg["model"] == "custom-x"   # существующий не перезаписан
