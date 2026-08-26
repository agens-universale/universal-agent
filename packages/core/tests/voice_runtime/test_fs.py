"""force_rmtree: снимает read-only (git-объекты Windows) и честно рапортует."""
import os
import stat

from gc_core.voice_runtime.fs import force_rmtree


def test_force_rmtree_removes_readonly_files(tmp_path):
    root = tmp_path / "clone"
    packs = root / ".git" / "objects" / "pack"
    packs.mkdir(parents=True)
    blob = packs / "pack-abc.pack"
    blob.write_bytes(b"x")
    os.chmod(blob, stat.S_IREAD)          # как делает git на Windows
    assert force_rmtree(root) is True
    assert not root.exists()


def test_force_rmtree_missing_dir_is_true(tmp_path):
    assert force_rmtree(tmp_path / "nope") is True
