"""canonical_path_key / orig_key / perimeter / protected skip."""
from __future__ import annotations

import os

import pytest

from gc_tui.diff_tui.paths import canonical_path_key, in_tracking_perimeter, is_trackable_path, orig_key


@pytest.fixture
def launch_root(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "foo.py").write_text("x", encoding="utf-8")
    return root


def test_canonical_key_relative_under_launch_root(launch_root):
    key = canonical_path_key(str(launch_root / "src" / "foo.py"), str(launch_root))
    assert key == "src/foo.py"


def test_canonical_key_accepts_relative_path(launch_root):
    key = canonical_path_key("src/foo.py", str(launch_root))
    assert key == "src/foo.py"


def test_canonical_key_outside_perimeter_returns_none(launch_root, tmp_path):
    outside = tmp_path / "other" / "a.py"
    outside.parent.mkdir()
    outside.write_text("x", encoding="utf-8")
    assert canonical_path_key(str(outside), str(launch_root)) is None
    assert not in_tracking_perimeter(str(outside), str(launch_root))


def test_canonical_key_extra_paths_uses_absolute_posix(launch_root, tmp_path):
    extra = tmp_path / "extra_root"
    extra.mkdir()
    target = extra / "lib.py"
    target.write_text("x", encoding="utf-8")
    key = canonical_path_key(
        str(target),
        str(launch_root),
        extra_paths=[str(extra)],
    )
    assert key is not None
    assert key == target.resolve().as_posix()
    assert key.startswith("/") or (os.name == "nt" and ":" in key)


def test_orig_key_is_sha256_hex_of_key():
    import hashlib

    key = "src/foo.py"
    expected = hashlib.sha256(key.encode("utf-8")).hexdigest()
    assert orig_key(key) == expected
    assert len(orig_key(key)) == 64


def test_orig_key_casefold_on_windows(monkeypatch):
    import hashlib

    monkeypatch.setattr(os, "name", "nt")
    key = "Src/Foo.py"
    expected = hashlib.sha256(key.casefold().encode("utf-8")).hexdigest()
    assert orig_key(key) == expected


def test_operational_gc_not_trackable(launch_root, monkeypatch):
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", launch_root.parent / "gc_home")
    gc_file = launch_root / ".gc" / "threads.json"
    gc_file.parent.mkdir(parents=True)
    gc_file.write_text("{}", encoding="utf-8")
    assert canonical_path_key(str(gc_file), str(launch_root)) is None
    assert not is_trackable_path(str(gc_file), str(launch_root))


def test_regular_file_is_trackable(launch_root, monkeypatch):
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", launch_root.parent / "gc_home")
    path = str(launch_root / "src" / "foo.py")
    assert is_trackable_path(path, str(launch_root))
    assert canonical_path_key(path, str(launch_root)) == "src/foo.py"
