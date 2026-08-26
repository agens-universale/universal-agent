"""read_guard на fs_find_in_files: запрещённый файл не читается и не попадает в ответ.

Паритет с list_files / find_files_by_name / старым search_files: там per-file гвард
стоял всегда. Здесь он выпал — тул фильтровал только каталоги, и содержимое .env,
приватных ключей и credentials*.json уходило модели.
"""
import json
import pytest

from tools.fs.find_in_files import FindInFiles
from tests.helpers.tool_ctx import with_local_ctx

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

    assert MARKER not in raw, "содержимое запрещённого файла утекло в ответ тула"
    files = [m.get("file", "") for m in data.get("matches", [])]
    for denied in (".env", "server.pem", "id.key"):
        assert not any(f.endswith(denied) for f in files), f"{denied} попал в matches"


def test_allowed_file_still_found(tree):
    """Гвард не должен глушить обычный поиск."""
    data = json.loads(with_local_ctx(FindInFiles, config_dict=_cfg(tree))(
        path=str(tree), pattern="SECRET", query_mode="python-re"))
    assert any(m.get("file", "").endswith("app.py") for m in data.get("matches", []))
