"""Native find/grep path equality smoke (gate-pass patterns)."""
import json

from tests.helpers.tool_ctx import with_local_ctx
from tools.fs.find_in_files import FindInFiles
from tools.fs.list_files import ListFiles
from tools.fs.find_files_by_name import FindFilesByName


def test_list_files_native_envelope(tmp_path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / "b.txt").write_text("y", encoding="utf-8")
    data = json.loads(with_local_ctx(ListFiles)(path=str(tmp_path)))
    assert data["ok"] is True
    assert data["count"] == 2
    assert data.get("listing_backend") in ("find", "python")


def test_find_by_name_native_envelope(tmp_path):
    (tmp_path / "hello_world.py").write_text("x", encoding="utf-8")
    data = json.loads(
        with_local_ctx(FindFilesByName)(path=str(tmp_path), query="hello")
    )
    assert data["ok"] is True
    assert data["count"] >= 1
    assert data.get("listing_backend") in ("find", "python")


def test_find_in_files_literal_native_or_python(tmp_path):
    (tmp_path / "a.py").write_text("TODO fix\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("other\n", encoding="utf-8")
    data = json.loads(
        with_local_ctx(FindInFiles)(
            path=str(tmp_path), pattern="TODO", query_mode="text"
        )
    )
    assert data["ok"] is True
    assert data["count"] == 1
    assert data.get("search_backend") in ("grep", "python")
    assert data["matches"][0]["line"] == 1


def test_find_in_files_denylist_stays_python(tmp_path):
    (tmp_path / "a.py").write_text("digit 12\n", encoding="utf-8")
    data = json.loads(
        with_local_ctx(FindInFiles)(
            path=str(tmp_path), pattern=r"\d+", query_mode="python-re"
        )
    )
    assert data["ok"] is True
    assert data.get("search_backend") == "python"
    assert data["count"] == 1
