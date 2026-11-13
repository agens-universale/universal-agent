import json
from pathlib import Path

import pytest
from gc_core.plugins.installed import (
    _safe_name,
    install,
    list_installed,
    uninstall,
)


def make_plugin(path, name):
    (path / ".claude-plugin").mkdir(parents=True)
    (path / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0"}), encoding="utf-8"
    )
    skill = path / "skills" / "s"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: s\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    return path


@pytest.mark.parametrize("name", ["..", "", "ABS", "unknown"])
def test_uninstall_refuses_unregistered_or_outside(tmp_path, name):
    store = tmp_path / "plugins" / "installed"
    install(str(make_plugin(tmp_path / "src" / "p", "p")), root=store)
    victim = tmp_path / "victim"
    victim.mkdir()
    name = str(victim) if name == "ABS" else name
    try:
        ok = uninstall(name, root=store)
    except ValueError:
        ok = False
    assert ok is False
    assert victim.exists() and (store / "p").is_dir()


def test_uninstall_refuses_tampered_registry_path(tmp_path):
    store = tmp_path / "installed"
    install(str(make_plugin(tmp_path / "src" / "p", "p")), root=store)
    victim = tmp_path / "victim"
    victim.mkdir()
    meta_file = store / ".installed.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    meta[".."] = meta["p"]
    meta_file.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(ValueError):
        uninstall("..", root=store)
    assert victim.exists() and (store / "p").is_dir()


def test_uninstall_removes_registered_plugin(tmp_path):
    store = tmp_path / "installed"
    install(str(make_plugin(tmp_path / "src" / "p", "p")), root=store)
    assert uninstall("p", root=store) is True
    assert not (store / "p").exists()
    assert list_installed(store) == []


def test_install_keeps_neighbours_named_like_staging(tmp_path):
    store = tmp_path / "installed"
    for n in ("p.new", "p.old"):
        install(str(make_plugin(tmp_path / "s" / n, n)), root=store)
    install(
        str(make_plugin(tmp_path / "s" / "p", "p")), root=store, force=True
    )
    assert [p.name for p in list_installed(store)] == ["p", "p.new", "p.old"]
    install(
        str(make_plugin(tmp_path / "s2" / "p", "p")), root=store, force=True
    )
    assert (store / "p.new" / ".claude-plugin").is_dir()
    assert (store / "p.old" / ".claude-plugin").is_dir()


def test_no_leftovers_after_install_and_update(tmp_path):
    from gc_core.plugins.installed import update

    store = tmp_path / "installed"
    install(str(make_plugin(tmp_path / "src" / "p", "p")), root=store)
    update("p", root=store)
    assert sorted(x.name for x in store.iterdir() if x.name != ".staging") == [
        ".installed.json",
        "p",
    ]
    assert list((store / ".staging").iterdir()) == []


def test_safe_name_never_yields_staging_dir():
    assert _safe_name(".staging") != ".staging"
    assert not _safe_name("..staging").startswith(".")


def test_failed_swap_restores_old_install(tmp_path, monkeypatch):
    store = tmp_path / "installed"
    install(str(make_plugin(tmp_path / "src" / "p", "p")), root=store)
    real_rename = Path.rename

    def broken(self, target):
        if self.name == "new":
            raise OSError("rename failed")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", broken)
    with pytest.raises(OSError):
        install(
            str(make_plugin(tmp_path / "s2" / "p", "p")),
            root=store,
            force=True,
        )
    monkeypatch.undo()
    assert (store / "p" / ".claude-plugin" / "plugin.json").is_file()
    assert list((store / ".staging").iterdir()) == []


def test_failed_update_write_keeps_plugin_disabled(tmp_path, monkeypatch):
    """Новая версия и прежний статус «выключен» публикуются одной записью:
    сбой следующей записи реестра не включает плагин."""
    from gc_core.plugins import installed

    store = tmp_path / "installed"
    install(str(make_plugin(tmp_path / "src" / "p", "p")), root=store)
    installed.disable("p", store)
    real_write = installed._write_meta
    writes = []

    def flaky(root, meta):
        writes.append(meta)
        if len(writes) > 1:
            raise OSError("disk full")
        real_write(root, meta)

    monkeypatch.setattr(installed, "_write_meta", flaky)
    installed.update("p", root=store)
    monkeypatch.undo()

    assert [p.enabled for p in list_installed(store)] == [False]
    assert installed.enabled_paths(store) == []
