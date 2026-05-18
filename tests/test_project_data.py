import os
import stat

import pytest

from utils.project_data import get_project_data


def test_get_project_data_default_includes_writable_flags(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data = get_project_data(None)
    assert data["projectDir"] == os.path.abspath(str(tmp_path))
    assert data["projectDirWritable"] in {"true", "false"}
    lines = [ln for ln in data["allowed_write_paths"].split("\n") if ln.strip()]
    assert os.path.abspath(str(tmp_path)) in lines


@pytest.mark.skipif(os.name == "nt", reason="POSIX chmod writable semantics")
def test_get_project_data_allowed_paths_skip_non_writable_project_dir(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    extra = tmp_path / "extra_allowed"
    extra.mkdir()
    old_mode = stat.S_IMODE(root.stat().st_mode)
    try:
        root.chmod(0o555)
        cfg = {
            "launch_root": str(root),
            "command_moderation": {
                "root_box": {"enabled": True, "extra_write_paths": [str(extra)]},
            },
        }
        data = get_project_data(cfg)
        assert data["projectDirWritable"] == "false"
        lines = [ln for ln in data["allowed_write_paths"].split("\n") if ln.strip()]
        assert os.path.abspath(str(root)) not in lines
        assert os.path.abspath(str(extra)) in lines
    finally:
        root.chmod(old_mode)


def test_get_project_data_merges_config_roots(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    extra = tmp_path / "extra_allowed"
    extra.mkdir()
    cfg = {
        "launch_root": str(root),
        "command_moderation": {
            "root_box": {"enabled": True, "extra_write_paths": [str(extra)]},
        },
    }
    data = get_project_data(cfg)
    assert data["projectDir"] == os.path.abspath(str(root))
    lines = data["allowed_write_paths"].split("\n")
    assert os.path.abspath(str(root)) in lines
    assert os.path.abspath(str(extra)) in lines
    assert data["projectDirWritable"] in {"true", "false"}
