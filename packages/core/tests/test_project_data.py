import os
import stat
import sys

import pytest
from gc_core.utils.project_data import get_project_data


def _boom_platform_system():
    raise RuntimeError("platform.system() called — WMI path forbidden in startup")


async def test_get_project_data_default_includes_writable_flags(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data = await get_project_data(None)
    assert data["projectDir"] == os.path.abspath(str(tmp_path))
    assert data["projectDirWritable"] in {"true", "false"}
    lines = [ln for ln in data["allowed_write_paths"].split("\n") if ln.strip()]
    assert os.path.abspath(str(tmp_path)) in lines


@pytest.mark.skipif(os.name == "nt", reason="POSIX chmod writable semantics")
async def test_get_project_data_allowed_paths_skip_non_writable_project_dir(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    extra = tmp_path / "extra_allowed"
    extra.mkdir()
    old_mode = stat.S_IMODE(root.stat().st_mode)
    try:
        root.chmod(0o555)
        cfg = {
            "launch_root": str(root),
            "write_guard": {"enabled": True, "extra_paths": [str(extra)]},
        }
        data = await get_project_data(cfg)
        assert data["projectDirWritable"] == "false"
        lines = [ln for ln in data["allowed_write_paths"].split("\n") if ln.strip()]
        assert os.path.abspath(str(root)) not in lines
        assert os.path.abspath(str(extra)) in lines
    finally:
        root.chmod(old_mode)


async def test_get_project_data_merges_config_roots(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    extra = tmp_path / "extra_allowed"
    extra.mkdir()
    cfg = {
        "launch_root": str(root),
        "write_guard": {"enabled": True, "extra_paths": [str(extra)]},
    }
    data = await get_project_data(cfg)
    assert data["projectDir"] == os.path.abspath(str(root))
    lines = data["allowed_write_paths"].split("\n")
    assert os.path.abspath(str(root)) in lines
    assert os.path.abspath(str(extra)) in lines
    assert data["projectDirWritable"] in {"true", "false"}


async def test_get_project_data_does_not_call_platform_system(tmp_path, monkeypatch):
    """Регрессия WMI-зависания: старт агента вис, т.к. get_project_data звал
    platform.system(), который на Windows+CPython 3.12 уходит в WMI (_wmi_query)
    и блокируется при подвисшей службе winmgmt. get_project_data не должен его звать."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("platform.system", _boom_platform_system)
    data = await get_project_data(None)
    assert data["OS"]  # не упало и вернуло непустую OS-строку


async def test_get_project_data_os_reflects_sys_platform(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert (await get_project_data(None))["OS"] == "macOS"


@pytest.mark.parametrize(
    "plat,expected",
    [("win32", "Windows"), ("linux", "Linux"), ("linux2", "Linux"), ("darwin", "macOS")],
)
def test_os_label_maps_sys_platform(monkeypatch, plat, expected):
    from gc_core.utils.project_data import os_label

    monkeypatch.setattr(sys, "platform", plat)
    assert os_label() == expected


def test_os_label_does_not_call_platform_system(monkeypatch):
    from gc_core.utils.project_data import os_label

    monkeypatch.setattr("platform.system", _boom_platform_system)
    assert os_label()  # непустая строка, без обращения к platform.system()
