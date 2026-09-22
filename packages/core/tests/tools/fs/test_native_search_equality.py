"""List/by-name walk enumeration + native grep smoke (gate-pass patterns)."""

import json

from gc_core.tools.fs.find_files_by_name import FindFilesByName
from gc_core.tools.fs.find_in_files import FindInFiles
from gc_core.tools.fs.list_files import ListFiles
from gc_testkit.helpers.tool_ctx import with_local_ctx


def test_list_files_walk_envelope(tmp_path):
    """List uses backend traverse only; no listing_backend (agent cannot act on it)."""
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / "b.txt").write_text("y", encoding="utf-8")
    data = json.loads(with_local_ctx(ListFiles)(path=str(tmp_path)))
    assert data["ok"] is True
    assert data["count"] == 2
    assert "listing_backend" not in data


def test_find_by_name_walk_envelope(tmp_path):
    """By-name uses iter_walk only; no tool-exec find dump."""
    (tmp_path / "hello_world.py").write_text("x", encoding="utf-8")
    data = json.loads(with_local_ctx(FindFilesByName)(path=str(tmp_path), query="hello"))
    assert data["ok"] is True
    assert data["count"] >= 1
    assert "listing_backend" not in data


def test_list_and_by_name_do_not_use_exec_for_tree(tmp_path, monkeypatch):
    """Regression A1: tree dump must not go through tool-exec (60k)."""
    from gc_core.backends.local import LocalFsBackend

    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("y", encoding="utf-8")

    calls: list = []
    real_exec_argv = LocalFsBackend.exec_argv

    def tracking_exec_argv(self, argv, **kwargs):
        calls.append(list(argv))
        return real_exec_argv(self, argv, **kwargs)

    monkeypatch.setattr(LocalFsBackend, "exec_argv", tracking_exec_argv)

    list_data = json.loads(with_local_ctx(ListFiles)(path=str(tmp_path), max_depth=2))
    assert list_data["ok"] is True
    by_name = json.loads(with_local_ctx(FindFilesByName)(path=str(tmp_path), query="*.py"))
    assert by_name["ok"] is True

    find_dumps = [c for c in calls if c and c[0] == "find" and any("-printf" in str(a) for a in c)]
    assert find_dumps == [], f"list/by-name must not exec find dump: {find_dumps}"


def test_find_in_files_literal_native_or_python(tmp_path):
    (tmp_path / "a.py").write_text("TODO fix\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("other\n", encoding="utf-8")
    data = json.loads(with_local_ctx(FindInFiles)(path=str(tmp_path), pattern="TODO", query_mode="text"))
    assert data["ok"] is True
    assert data["count"] == 1
    assert data.get("search_backend") in ("grep", "python")
    assert data["matches"][0]["line"] == 1


def test_find_in_files_denylist_stays_python(tmp_path):
    (tmp_path / "a.py").write_text("digit 12\n", encoding="utf-8")
    data = json.loads(with_local_ctx(FindInFiles)(path=str(tmp_path), pattern=r"\d+", query_mode="python-re"))
    assert data["ok"] is True
    assert data.get("search_backend") == "python"
    assert data["count"] == 1
