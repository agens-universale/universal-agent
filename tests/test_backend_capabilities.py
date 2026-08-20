"""Capability probes and FakeBackend overrides."""
from backends.local import LocalFsBackend
from tests.harbor_support.fakes import FakeBackend


def test_fake_backend_default_caps_false():
    b = FakeBackend({"/work/a.txt": "x"})
    assert b.has_find is False
    assert b.has_grep is False
    assert b.grep_exclude_dir is False
    assert b.grep_include is False


def test_fake_backend_override_caps():
    b = FakeBackend(
        {"/work/a.txt": "x"},
        capabilities={
            "has_find": True,
            "has_grep": True,
            "grep_exclude_dir": True,
            "grep_include": True,
        },
    )
    assert b.has_find is True
    assert b.has_grep is True


def test_local_has_find_grep_smoke_cache():
    b = LocalFsBackend()
    # On a typical Linux CI/dev host GNU find/grep exist.
    f1 = b.has_find
    f2 = b.has_find
    assert f1 is f2
    g1 = b.has_grep
    g2 = b.has_grep
    assert g1 is g2
    # Values are bool (probe may fail in minimal env — still no raise)
    assert isinstance(f1, bool)
    assert isinstance(g1, bool)
