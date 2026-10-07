from types import SimpleNamespace

from gc_core.project_memory import get_hash, resolve_project_memory_path
from gc_core.project_memory.storage import content_hash


def test_content_hash_missing_file_returns_none(tmp_path):
    assert content_hash(tmp_path / "project_memory.md") is None


def test_content_hash_empty_or_whitespace_returns_none(tmp_path):
    p = tmp_path / "project_memory.md"
    p.write_text("", encoding="utf-8")
    assert content_hash(p) is None
    p.write_text("   \n\t  ", encoding="utf-8")
    assert content_hash(p) is None


def test_content_hash_stable_for_same_content(tmp_path):
    p = tmp_path / "project_memory.md"
    p.write_text("decision: use pytest\n", encoding="utf-8")
    h1 = content_hash(p)
    h2 = content_hash(p)
    assert h1 is not None
    assert h1 == h2
    assert len(h1) == 64


def test_content_hash_changes_when_content_changes(tmp_path):
    p = tmp_path / "project_memory.md"
    p.write_text("v1", encoding="utf-8")
    h1 = content_hash(p)
    p.write_text("v2", encoding="utf-8")
    h2 = content_hash(p)
    assert h1 is not None and h2 is not None
    assert h1 != h2


def test_get_hash_uses_ctx_project_gc_dir(tmp_path):
    gc = tmp_path / ".gc"
    gc.mkdir()
    (gc / "project_memory.md").write_text("project fact", encoding="utf-8")
    ctx = SimpleNamespace(project_gc_dir=gc)
    assert get_hash(ctx) == content_hash(gc / "project_memory.md")


def test_resolve_project_memory_path_from_ctx(tmp_path):
    gc = tmp_path / "workspace" / ".gc"
    gc.mkdir(parents=True)
    ctx = SimpleNamespace(project_gc_dir=gc)
    assert resolve_project_memory_path(ctx) == gc / "project_memory.md"
