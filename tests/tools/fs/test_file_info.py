import json

from tools.fs.file_info import FileInfo
from utils.file_text import sha256_hex_of_text


def test_file_info_missing_path(tmp_path):
    missing = tmp_path / "nope.txt"
    res = json.loads(FileInfo()(path=str(missing)))
    assert res["ok"] is True
    assert res["exists"] is False
    assert res["sha256"] is None


def test_file_info_fast_defaults(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("hello\nworld\n", encoding="utf-8")
    res = json.loads(FileInfo()(path=str(p)))
    assert res["ok"] is True
    assert res["exists"] is True
    assert res["type"] == "file"
    assert res["size_bytes"] == len("hello\nworld\n")
    assert res["is_binary"] is False
    assert res["encoding_guess"] == "utf-8"
    assert res["sha256"] is None
    assert res["line_count"] is None


def test_file_info_with_hash_and_line_count(tmp_path):
    text = "alpha\nbeta\n"
    p = tmp_path / "b.txt"
    p.write_text(text, encoding="utf-8")
    res = json.loads(
        FileInfo()(
            path=str(p),
            include_hash=True,
            include_line_count=True,
        )
    )
    assert res["sha256"] == sha256_hex_of_text(text)
    assert res["line_count"] == 2

