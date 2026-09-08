"""read_guard на fs_find_in_files: запрещённый файл не читается и не попадает в ответ.

Паритет с list_files / find_files_by_name / старым search_files: там per-file гвард
стоял всегда. Здесь он выпал — тул фильтровал только каталоги, и содержимое .env,
приватных ключей и credentials*.json уходило модели.
"""
import inspect
import json
import pytest

from gc_core.tools.fs.find_in_files import FindInFiles
from gc_testkit.helpers.tool_ctx import with_local_ctx
from gc_core.utils import native_fs_search as native_fs_search

MARKER = "sk-MUST-NOT-LEAK"


def _cfg(root):
    return {
        "launch_root": str(root),
        "root_box": {"enabled": True, "launch_root": str(root)},
        "read_guard": {
            "enabled": True,
            "mode": "deny",
            "denied_file_patterns": [".env", ".env.*", "*.pem", "*id_rsa*"],
            "denied_extensions": [".key", ".pfx"],
        },
    }


def _assert_no_denied_leak(raw: str, data: dict) -> None:
    assert MARKER not in raw, "содержимое запрещённого файла утекло в ответ тула"
    files = [m.get("file", "") for m in data.get("matches", [])]
    denied_names = (".env", "server.pem", "id.key")
    for denied in denied_names:
        assert not any(f.endswith(denied) for f in files), f"{denied} попал в matches"
    for m in data.get("matches", []):
        for field in ("line_content", "context"):
            assert MARKER not in (m.get(field) or "")
    for denied in denied_names:
        assert f"/{denied}" not in raw
        assert f"/{denied}\"" not in raw


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "app.py").write_text("TOKEN = os.environ['SECRET']\n", encoding="utf-8")
    (tmp_path / ".env").write_text(f"SECRET={MARKER}\n", encoding="utf-8")
    (tmp_path / "server.pem").write_text(f"PRIVATE {MARKER}\n", encoding="utf-8")
    (tmp_path / "id.key").write_text(f"KEY {MARKER}\n", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("has_grep", [False, True], ids=["python-path", "native-grep"])
def test_denied_files_never_reach_the_payload(tree, monkeypatch, has_grep):
    tool = with_local_ctx(FindInFiles, config_dict=_cfg(tree))
    monkeypatch.setattr(type(tool._backend), "has_grep", has_grep, raising=False)

    raw = tool(path=str(tree), pattern="SECRET|PRIVATE|KEY", query_mode="python-re")
    data = json.loads(raw)

    _assert_no_denied_leak(raw, data)
    if has_grep:
        assert data.get("search_backend") == "grep"


@pytest.mark.parametrize("has_grep", [False, True], ids=["python-path", "native-grep"])
def test_denied_files_text_mode_never_reach_the_payload(tree, monkeypatch, has_grep):
    tool = with_local_ctx(FindInFiles, config_dict=_cfg(tree))
    monkeypatch.setattr(type(tool._backend), "has_grep", has_grep, raising=False)

    raw = tool(path=str(tree), pattern="KEY", query_mode="text")
    data = json.loads(raw)

    _assert_no_denied_leak(raw, data)
    if has_grep:
        assert data.get("search_backend") == "grep"


def test_native_denied_hits_first_allowed_still_visible_or_incomplete(tmp_path, monkeypatch):
    """Первые hits из .env; разрешённый тоже матчится — в matches или scan неполный."""
    (tmp_path / ".env").write_text(
        "\n".join(f"KEY={MARKER}_{i}" for i in range(8)) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "ok.py").write_text('KEY = "visible"\n', encoding="utf-8")

    tool = with_local_ctx(FindInFiles, config_dict=_cfg(tmp_path))
    monkeypatch.setattr(type(tool._backend), "has_grep", True, raising=False)

    raw = tool(path=str(tmp_path), pattern="KEY", query_mode="text", limit=2)
    data = json.loads(raw)

    assert data.get("search_backend") == "grep"
    assert MARKER not in raw
    files = [m.get("file", "") for m in data.get("matches", [])]
    assert not any(f.endswith(".env") for f in files)
    assert data["count"] == len(data["matches"])
    assert data["count"] <= 2
    allowed = any(f.endswith("ok.py") for f in files)
    if not allowed:
        assert data["scan_complete"] is False
        assert data["more_matches_may_exist"] is True
        assert data.get("stop_reason") != "match_limit"


def test_native_only_denied_matches_scan_complete(tmp_path, monkeypatch):
    """Grep не упёрся в лимит, совпали только denied — пусто и scan_complete."""
    (tmp_path / ".env").write_text(f"KEY={MARKER}\n", encoding="utf-8")
    (tmp_path / "ok.py").write_text("nothing here\n", encoding="utf-8")

    tool = with_local_ctx(FindInFiles, config_dict=_cfg(tmp_path))
    monkeypatch.setattr(type(tool._backend), "has_grep", True, raising=False)

    raw = tool(path=str(tmp_path), pattern="KEY", query_mode="text")
    data = json.loads(raw)

    assert data.get("search_backend") == "grep"
    assert MARKER not in raw
    assert data["matches"] == []
    assert data["count"] == 0
    assert data["scan_complete"] is True
    assert data["more_matches_may_exist"] is False
    assert "stop_reason" not in data


def test_allowed_file_still_found(tree):
    """Гвард не должен глушить обычный поиск."""
    data = json.loads(with_local_ctx(FindInFiles, config_dict=_cfg(tree))(
        path=str(tree), pattern="SECRET", query_mode="python-re"))
    assert any(m.get("file", "").endswith("app.py") for m in data.get("matches", []))


def test_native_fs_search_has_no_read_guard_import():
    src = inspect.getsource(native_fs_search)
    assert "read_guard" not in src
